import json
import uuid

import pytest
from app.core.config import get_settings
from app.services import webhooks as webhook_service
from app.services.webhooks import (
    UnsafeWebhookURL,
    assert_public_https_url,
    create_deliveries,
    sign,
)
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import Call


async def _signup(client: AsyncClient, *, slug: str) -> dict:
    resp = await client.post(
        "/api/v1/auth/signup",
        json={
            "company_name": slug.title(),
            "tenant_slug": slug,
            "full_name": "Owner",
            "email": f"owner@{slug}.com",
            "password": "supersecret1",
        },
    )
    assert resp.status_code == 201, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _make_key(client: AsyncClient, owner: dict, *, mode: str, scopes: list[str]) -> dict:
    resp = await client.post(
        "/api/v1/developer/api-keys",
        headers=owner,
        json={"name": f"{mode} key", "mode": mode, "scopes": scopes},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _bearer(key: dict) -> dict:
    return {"Authorization": f"Bearer {key['secret']}"}


async def _agent_id(client: AsyncClient, owner: dict) -> str:
    resp = await client.post("/api/v1/ai-agents", headers=owner, json={"name": "Receptionist"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest.fixture
def public_dns(monkeypatch):
    """Webhook URL checks resolve DNS; pin it to a public address so the tests
    don't depend on the network."""
    monkeypatch.setattr(
        webhook_service.socket,
        "getaddrinfo",
        lambda host, port, **kw: [(2, 1, 6, "", ("93.184.216.34", port))],
    )


# --- API keys ---------------------------------------------------------------


async def test_key_secret_shown_once_and_authenticates(client: AsyncClient) -> None:
    owner = await _signup(client, slug="keys-co")
    key = await _make_key(client, owner, mode="live", scopes=["calls:read"])
    assert key["secret"].startswith("ak_live_")
    assert key["prefix"] == key["secret"][: len(key["prefix"])]

    listed = (await client.get("/api/v1/developer/api-keys", headers=owner)).json()
    assert len(listed) == 1 and "secret" not in listed[0]

    assert (await client.get("/api/v1/calls", headers=_bearer(key))).status_code == 200


async def test_key_scopes_limit_what_it_can_do(client: AsyncClient) -> None:
    owner = await _signup(client, slug="scopes-co")
    read_only = await _make_key(client, owner, mode="test", scopes=["calls:read"])

    # calls:read can list but not place calls, and reaches nothing outside its scopes.
    assert (await client.get("/api/v1/calls", headers=_bearer(read_only))).status_code == 200
    place = await client.post(
        "/api/v1/calls",
        headers=_bearer(read_only),
        json={"direction": "inbound", "from_number": "+15550001111"},
    )
    assert place.status_code == 403
    assert (await client.get("/api/v1/customers", headers=_bearer(read_only))).status_code == 403
    assert (
        await client.get("/api/v1/billing/plans", headers=_bearer(read_only))
    ).status_code == 403
    assert (await client.get("/api/v1/users/me", headers=_bearer(read_only))).status_code == 403


async def test_key_cannot_manage_keys(client: AsyncClient) -> None:
    owner = await _signup(client, slug="nomint-co")
    key = await _make_key(client, owner, mode="live", scopes=["calls:write", "webhooks:manage"])
    resp = await client.post(
        "/api/v1/developer/api-keys",
        headers=_bearer(key),
        json={"name": "x", "mode": "live", "scopes": ["calls:read"]},
    )
    assert resp.status_code == 403


async def test_revoked_and_bogus_keys_are_rejected(client: AsyncClient) -> None:
    owner = await _signup(client, slug="revoke-co")
    key = await _make_key(client, owner, mode="live", scopes=["calls:read"])
    assert (await client.get("/api/v1/calls", headers=_bearer(key))).status_code == 200

    assert (
        await client.delete(f"/api/v1/developer/api-keys/{key['id']}", headers=owner)
    ).status_code == 204
    assert (await client.get("/api/v1/calls", headers=_bearer(key))).status_code == 401
    bogus = {"Authorization": "Bearer ak_live_notarealkey"}
    assert (await client.get("/api/v1/calls", headers=bogus)).status_code == 401


async def test_keys_are_tenant_isolated(client: AsyncClient) -> None:
    owner_a = await _signup(client, slug="tenant-a")
    owner_b = await _signup(client, slug="tenant-b")
    key_a = await _make_key(client, owner_a, mode="test", scopes=["calls:write", "calls:read"])
    agent_a = await _agent_id(client, owner_a)
    await client.post(
        "/api/v1/calls",
        headers=_bearer(key_a),
        json={
            "direction": "outbound",
            "to_number": "+919876543210",
            "from_number": "+14155550100",
            "ai_agent_id": agent_a,
        },
    )
    # Tenant B's own session sees none of tenant A's calls.
    assert (await client.get("/api/v1/calls", headers=owner_b)).json() == []
    assert len((await client.get("/api/v1/calls", headers=_bearer(key_a))).json()) == 1


async def test_rate_limit_returns_429_with_retry_after(client: AsyncClient) -> None:
    owner = await _signup(client, slug="ratelimit-co")
    key = await _make_key(client, owner, mode="test", scopes=["calls:read"])
    settings = get_settings()
    original = settings.api_key_rate_limit_per_minute
    settings.api_key_rate_limit_per_minute = 2
    try:
        statuses = [
            (await client.get("/api/v1/calls", headers=_bearer(key))).status_code for _ in range(4)
        ]
        limited = await client.get("/api/v1/calls", headers=_bearer(key))
    finally:
        settings.api_key_rate_limit_per_minute = original
    assert statuses[:2] == [200, 200]
    assert 429 in statuses
    assert limited.status_code == 429 and int(limited.headers["Retry-After"]) > 0


# --- Calls via API key: test mode, idempotency ------------------------------


async def test_test_mode_key_never_dials(client: AsyncClient, db_session: AsyncSession) -> None:
    owner = await _signup(client, slug="testmode-co")
    key = await _make_key(client, owner, mode="test", scopes=["calls:write", "calls:read"])
    agent = await _agent_id(client, owner)
    # Twilio is unconfigured in tests, so a live dial would 424; test mode must not try.
    resp = await client.post(
        "/api/v1/calls",
        headers=_bearer(key),
        json={
            "direction": "outbound",
            "to_number": "+919876543210",
            "from_number": "+14155550100",
            "ai_agent_id": agent,
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "completed"
    assert body.get("provider_call_sid") in (None, "")
    # Test calls stay out of the CRM.
    assert (await client.get("/api/v1/customers", headers=owner)).json() == []


async def test_idempotency_key_returns_same_call(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    owner = await _signup(client, slug="idem-co")
    key = await _make_key(client, owner, mode="test", scopes=["calls:write", "calls:read"])
    agent = await _agent_id(client, owner)
    body = {
        "direction": "outbound",
        "to_number": "+919876543210",
        "from_number": "+14155550100",
        "ai_agent_id": agent,
    }
    headers = {**_bearer(key), "Idempotency-Key": "order-1234"}
    first = await client.post("/api/v1/calls", headers=headers, json=body)
    second = await client.post("/api/v1/calls", headers=headers, json=body)
    other = await client.post(
        "/api/v1/calls", headers={**_bearer(key), "Idempotency-Key": "order-9999"}, json=body
    )
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert other.json()["id"] != first.json()["id"]
    total = (await db_session.execute(select(func.count(Call.id)))).scalar_one()
    assert total == 2


async def test_concurrent_call_cap(client: AsyncClient) -> None:
    owner = await _signup(client, slug="cap-co")
    agent = await _agent_id(client, owner)
    settings = get_settings()
    original = settings.max_concurrent_calls_per_tenant
    settings.max_concurrent_calls_per_tenant = 1
    try:
        # An in-flight (queued) inbound-logged call fills the single slot.
        await client.post(
            "/api/v1/calls",
            headers=owner,
            json={"direction": "inbound", "from_number": "+15550001111", "ai_agent_id": agent},
        )
        blocked = await client.post(
            "/api/v1/calls",
            headers=owner,
            json={
                "direction": "outbound",
                "to_number": "+919876543210",
                "from_number": "+14155550100",
                "ai_agent_id": agent,
            },
        )
    finally:
        settings.max_concurrent_calls_per_tenant = original
    assert blocked.status_code == 429


# --- Webhooks ---------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/hook",  # not https
        "https://localhost/hook",
        "https://127.0.0.1/hook",
        "https://169.254.169.254/latest/meta-data",  # cloud metadata
        "https://10.0.0.5/hook",
        "https://[::1]/hook",
        "https://user:pw@example.com/hook",
    ],
)
def test_unsafe_webhook_urls_rejected(url: str) -> None:
    with pytest.raises(UnsafeWebhookURL):
        assert_public_https_url(url)


async def test_create_webhook_rejects_private_target(client: AsyncClient) -> None:
    owner = await _signup(client, slug="ssrf-co")
    resp = await client.post(
        "/api/v1/developer/webhooks",
        headers=owner,
        json={"url": "https://169.254.169.254/latest", "events": ["call.ended"]},
    )
    assert resp.status_code == 400


async def test_webhook_crud_and_secret_only_once(client: AsyncClient, public_dns) -> None:
    owner = await _signup(client, slug="hooks-co")
    created = await client.post(
        "/api/v1/developer/webhooks",
        headers=owner,
        json={"url": "https://example.com/hook", "events": ["call.ended", "ticket.created"]},
    )
    assert created.status_code == 201, created.text
    hook = created.json()
    assert hook["signing_secret"].startswith("whsec_")

    listed = (await client.get("/api/v1/developer/webhooks", headers=owner)).json()
    assert len(listed) == 1 and "signing_secret" not in listed[0]

    patched = await client.patch(
        f"/api/v1/developer/webhooks/{hook['id']}", headers=owner, json={"enabled": False}
    )
    assert patched.json()["enabled"] is False
    assert (
        await client.delete(f"/api/v1/developer/webhooks/{hook['id']}", headers=owner)
    ).status_code == 204
    assert (await client.get("/api/v1/developer/webhooks", headers=owner)).json() == []


async def test_webhook_test_delivery_is_signed(
    client: AsyncClient, public_dns, monkeypatch
) -> None:
    owner = await _signup(client, slug="sign-co")
    hook = (
        await client.post(
            "/api/v1/developer/webhooks",
            headers=owner,
            json={"url": "https://example.com/hook", "events": ["call.ended"]},
        )
    ).json()

    seen: dict = {}

    class FakeResponse:
        status_code = 200

    class FakeClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, content, headers):
            seen.update(url=url, body=content, headers=headers)
            return FakeResponse()

    monkeypatch.setattr(webhook_service.httpx, "AsyncClient", FakeClient)
    resp = await client.post(f"/api/v1/developer/webhooks/{hook['id']}/test", headers=owner)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "succeeded" and resp.json()["response_code"] == 200

    headers = seen["headers"]
    expected = sign(hook["signing_secret"], int(headers["X-Timestamp"]), seen["body"])
    assert headers["X-Signature"] == expected
    payload = json.loads(seen["body"])
    assert payload["event"] == "webhook.test" and payload["id"] == headers["X-Delivery-Id"]

    log = (await client.get("/api/v1/developer/webhooks/deliveries", headers=owner)).json()
    assert len(log) == 1 and log[0]["status"] == "succeeded"


async def test_failed_delivery_is_recorded(client: AsyncClient, public_dns, monkeypatch) -> None:
    owner = await _signup(client, slug="fail-co")
    hook = (
        await client.post(
            "/api/v1/developer/webhooks",
            headers=owner,
            json={"url": "https://example.com/hook", "events": ["call.ended"]},
        )
    ).json()

    class FakeResponse:
        status_code = 500

    class FakeClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, content, headers):
            return FakeResponse()

    monkeypatch.setattr(webhook_service.httpx, "AsyncClient", FakeClient)
    resp = await client.post(f"/api/v1/developer/webhooks/{hook['id']}/test", headers=owner)
    assert resp.json()["status"] == "failed" and resp.json()["response_code"] == 500


async def test_fan_out_only_to_subscribed_enabled_endpoints(
    client: AsyncClient, db_session: AsyncSession, public_dns
) -> None:
    owner = await _signup(client, slug="fanout-co")
    me = (await client.get("/api/v1/tenants/me", headers=owner)).json()
    tenant_id = uuid.UUID(me["id"])

    async def add(events: list[str]) -> dict:
        return (
            await client.post(
                "/api/v1/developer/webhooks",
                headers=owner,
                json={"url": "https://example.com/hook", "events": events},
            )
        ).json()

    wants_ended = await add(["call.ended"])
    await add(["ticket.created"])
    disabled = await add(["call.ended"])
    await client.patch(
        f"/api/v1/developer/webhooks/{disabled['id']}", headers=owner, json={"enabled": False}
    )

    ids = await create_deliveries(
        db_session, tenant_id=tenant_id, event="call.ended", data={"call_id": "c1"}
    )
    assert len(ids) == 1
    await db_session.commit()
    log = (await client.get("/api/v1/developer/webhooks/deliveries", headers=owner)).json()
    assert [d["endpoint_id"] for d in log] == [wants_ended["id"]]


# --- Usage ------------------------------------------------------------------


async def test_usage_counts_api_requests(client: AsyncClient) -> None:
    owner = await _signup(client, slug="usage-co")
    key = await _make_key(client, owner, mode="live", scopes=["calls:read"])
    for _ in range(3):
        assert (await client.get("/api/v1/calls", headers=_bearer(key))).status_code == 200

    usage = (await client.get("/api/v1/developer/usage", headers=owner)).json()
    assert usage["api_requests"] == 3
    assert usage["quota"]["api_requests_per_minute"] == get_settings().api_key_rate_limit_per_minute
    assert usage["daily"][0]["api_requests"] == 3
    assert {"call_minutes", "tts_characters", "whatsapp_messages", "estimated_cost_paise"} <= set(
        usage
    )


async def test_developer_endpoints_require_permission(client: AsyncClient) -> None:
    assert (await client.get("/api/v1/developer/api-keys")).status_code == 401


# --- Inbound + outbound through the API -------------------------------------


@pytest.fixture
def captured_events(monkeypatch):
    events: list[tuple[str, dict]] = []

    def fake(*, tenant_id, event, data):
        events.append((event, data))

    from app.api.v1 import calls as calls_module
    from app.services import call_finalize

    monkeypatch.setattr(calls_module, "enqueue_webhook_event", fake)
    monkeypatch.setattr(call_finalize, "enqueue_webhook_event", fake)
    return events


@pytest.mark.parametrize("direction", ["outbound", "inbound"])
async def test_test_mode_runs_full_lifecycle_both_directions(
    client: AsyncClient, captured_events, direction: str
) -> None:
    owner = await _signup(client, slug=f"life-{direction}")
    key = await _make_key(client, owner, mode="test", scopes=["calls:write", "calls:read"])
    agent = await _agent_id(client, owner)
    body = {
        "direction": direction,
        "from_number": "+14155550100",
        "to_number": "+919876543210",
        "ai_agent_id": agent,
    }
    resp = await client.post("/api/v1/calls", headers=_bearer(key), json=body)
    assert resp.status_code == 201, resp.text
    assert resp.json()["direction"] == direction and resp.json()["status"] == "completed"

    names = [name for name, _ in captured_events]
    assert names[:2] == ["call.started", "call.ended"]
    started = dict(captured_events[0][1])
    assert started["direction"] == direction and started["test_mode"] is True


async def test_list_calls_filters_by_direction_and_status(client: AsyncClient) -> None:
    owner = await _signup(client, slug="filters-co")
    key = await _make_key(client, owner, mode="test", scopes=["calls:write", "calls:read"])
    agent = await _agent_id(client, owner)
    for direction in ("inbound", "inbound", "outbound"):
        await client.post(
            "/api/v1/calls",
            headers=_bearer(key),
            json={
                "direction": direction,
                "from_number": "+14155550100",
                "to_number": "+919876543210",
                "ai_agent_id": agent,
            },
        )
    inbound = (
        await client.get("/api/v1/calls", headers=owner, params={"direction": "inbound"})
    ).json()
    outbound = (
        await client.get("/api/v1/calls", headers=owner, params={"direction": "outbound"})
    ).json()
    completed = (
        await client.get("/api/v1/calls", headers=owner, params={"status": "completed"})
    ).json()
    failed = (await client.get("/api/v1/calls", headers=owner, params={"status": "failed"})).json()
    assert (len(inbound), len(outbound), len(completed), len(failed)) == (2, 1, 3, 0)


async def test_phone_number_routing_via_api_key(client: AsyncClient) -> None:
    owner = await _signup(client, slug="numbers-co")
    writer = await _make_key(client, owner, mode="live", scopes=["numbers:write"])
    reader = await _make_key(client, owner, mode="live", scopes=["numbers:read"])
    agent_a = await _agent_id(client, owner)
    agent_b = (
        await client.post("/api/v1/ai-agents", headers=owner, json={"name": "Billing"})
    ).json()["id"]

    created = await client.post(
        "/api/v1/phone-numbers",
        headers=_bearer(writer),
        json={"e164_number": "+14155550123", "ai_agent_id": agent_a},
    )
    assert created.status_code == 201, created.text
    number_id = created.json()["id"]

    # Re-route inbound calls to another agent, then to none.
    moved = await client.patch(
        f"/api/v1/phone-numbers/{number_id}", headers=_bearer(writer), json={"ai_agent_id": agent_b}
    )
    assert moved.json()["ai_agent_id"] == agent_b
    cleared = await client.patch(
        f"/api/v1/phone-numbers/{number_id}", headers=_bearer(writer), json={"ai_agent_id": None}
    )
    assert cleared.json()["ai_agent_id"] is None

    # A read-only key can list but not change anything.
    assert (await client.get("/api/v1/phone-numbers", headers=_bearer(reader))).status_code == 200
    denied = await client.patch(
        f"/api/v1/phone-numbers/{number_id}", headers=_bearer(reader), json={"ai_agent_id": agent_a}
    )
    assert denied.status_code == 403

    assert (
        await client.delete(f"/api/v1/phone-numbers/{number_id}", headers=_bearer(writer))
    ).status_code == 204
    assert (await client.get("/api/v1/phone-numbers", headers=owner)).json() == []


async def test_duplicate_number_is_409_not_500(client: AsyncClient) -> None:
    owner_a = await _signup(client, slug="dup-a")
    owner_b = await _signup(client, slug="dup-b")
    first = await client.post(
        "/api/v1/phone-numbers", headers=owner_a, json={"e164_number": "+14155550199"}
    )
    second = await client.post(
        "/api/v1/phone-numbers", headers=owner_b, json={"e164_number": "+14155550199"}
    )
    assert first.status_code == 201 and second.status_code == 409


async def test_cannot_use_another_tenants_agent(client: AsyncClient) -> None:
    owner_a = await _signup(client, slug="agent-a")
    owner_b = await _signup(client, slug="agent-b")
    foreign_agent = await _agent_id(client, owner_b)

    # Routing a number to it
    number = await client.post(
        "/api/v1/phone-numbers",
        headers=owner_a,
        json={"e164_number": "+14155550177", "ai_agent_id": foreign_agent},
    )
    assert number.status_code == 404
    own = await client.post(
        "/api/v1/phone-numbers", headers=owner_a, json={"e164_number": "+14155550178"}
    )
    patched = await client.patch(
        f"/api/v1/phone-numbers/{own.json()['id']}",
        headers=owner_a,
        json={"ai_agent_id": foreign_agent},
    )
    assert patched.status_code == 404

    # ...or running it on a call
    call = await client.post(
        "/api/v1/calls",
        headers=owner_a,
        json={
            "direction": "outbound",
            "to_number": "+919876543210",
            "from_number": "+14155550100",
            "ai_agent_id": foreign_agent,
        },
    )
    assert call.status_code == 404
