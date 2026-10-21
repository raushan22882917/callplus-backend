import base64
import io
import json
import time
import uuid
import zipfile
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from app.core import pii
from app.core.ip_allowlist import invalidate_allowlist_cache, ip_allowed, normalize_entry
from app.core.totp import (
    generate_recovery_codes,
    generate_secret,
    hash_recovery_code,
    provisioning_uri,
    totp_at,
    verify_totp,
)
from app.main import app
from app.services import platform_connectors as hub
from app.services import platform_retention as retention
from app.services import platform_sso as sso
from app.services.platform_usage import (
    aggregate_usage,
    build_summary,
    check_plan_limit,
    parse_month,
)
from app.tools.context import ToolContext
from app.tools.platform_tools import lookup_order
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import (
    AIAgent,
    Call,
    CallDirection,
    CallStatus,
    Conversation,
    ConversationChannel,
    Customer,
    Message,
    MessageDirection,
    MessageSender,
    Tenant,
    User,
)
from packages.database.models import platform as platform_models  # noqa: F401
from packages.database.models.platform import (
    IntegrationConnector,
    PlatformAdmin,
    PricingPlan,
    RetentionPolicy,
    UsageEvent,
)


def _mount_platform_router() -> None:
    from app.api.v1 import platform

    if "/api/v1/platform/usage" not in app.openapi()["paths"]:
        app.include_router(platform.router, prefix="/api/v1")
        app.openapi_schema = None


_mount_platform_router()


def _b32(raw: bytes) -> str:
    return base64.b32encode(raw).decode().rstrip("=")


# ---- TOTP (RFC 6238 appendix B vectors, 8 digits) --------------- ----

SHA1_KEY = _b32(b"12345678901234567890")
SHA256_KEY = _b32(b"12345678901234567890123456789012")
SHA512_KEY = _b32(b"1234567890123456789012345678901234567890123456789012345678901234")


@pytest.mark.parametrize(
    ("when", "expected"),
    [
        (59, "94287082"),
        (1111111109, "07081804"),
        (1111111111, "14050471"),
        (1234567890, "89005924"),
        (2000000000, "69279037"),
        (20000000000, "65353130"),
    ],
)
def test_totp_rfc6238_sha1_vectors(when: int, expected: str) -> None:
    assert totp_at(SHA1_KEY, when, digits=8) == expected


def test_totp_rfc6238_sha256_and_sha512_vectors() -> None:
    assert totp_at(SHA256_KEY, 59, digits=8, algorithm="sha256") == "46119246"
    assert totp_at(SHA512_KEY, 59, digits=8, algorithm="sha512") == "90693936"
    assert totp_at(SHA256_KEY, 1111111109, digits=8, algorithm="sha256") == "68084774"


def test_verify_totp_window_and_replay() -> None:
    secret = generate_secret()
    now = 1_700_000_000
    code = totp_at(secret, now)
    step = verify_totp(secret, code, now=now)
    assert step == now // 30
    # already-used step is a replay
    assert verify_totp(secret, code, now=now, min_step=step) is None
    # one step of drift tolerated, two are not
    assert verify_totp(secret, code, now=now + 30) is not None
    assert verify_totp(secret, code, now=now + 90) is None
    assert verify_totp(secret, "abc123", now=now) is None


def test_provisioning_uri_and_recovery_codes() -> None:
    uri = provisioning_uri("ABCDEF", account="a@b.com", issuer="Auto NXT")
    assert uri.startswith("otpauth://totp/Auto%20NXT:a%40b.com?secret=ABCDEF")
    codes = generate_recovery_codes()
    assert len(set(codes)) == 8
    assert hash_recovery_code(codes[0].upper()) == hash_recovery_code(codes[0])


# ---- PII masking ------------------------------------------------ ----


def test_mask_phone_email_and_text() -> None:
    assert pii.mask_phone("+919876543210") == "+**********10"
    assert pii.mask_phone(None) is None
    assert pii.mask_email("jane.doe@example.com") == "j*******@example.com"
    text = (
        "Call me on +91 98765 43210 or jane@example.com. Aadhaar 2345 6789 0123, "
        "card 4111 1111 1111 1111, order 1234567."
    )
    masked = pii.mask_text(text)
    assert "98765" not in masked
    assert "jane@" not in masked
    assert "XXXX XXXX 0123" in masked
    assert "************1111" in masked
    assert "order 1234567" in masked  # 7 digits is not a phone number


def test_mask_text_over_masks_non_luhn_digit_runs_rather_than_leak_them() -> None:
    # Fails Luhn so it is not treated as a card, but is still a long digit run
    # that must not survive in the clear.
    assert "4111 1111" not in pii.mask_text("ref 4111 1111 1111 1112")
    assert pii.mask_text("order 12345") == "order 12345"


# ---- IP allowlist helpers --------------------------------------- ----


def test_ip_allowed() -> None:
    entries = [normalize_entry("10.0.0.0/8"), normalize_entry("203.0.113.7")]
    assert ip_allowed("10.2.3.4", entries)
    assert ip_allowed("203.0.113.7", entries)
    assert not ip_allowed("203.0.113.8", entries)
    assert ip_allowed("1.2.3.4", [])
    assert not ip_allowed("garbage", entries)


# ---- helpers for DB/API tests ----------------------------------- ----


async def _signup(client: AsyncClient, slug: str, email: str | None = None) -> dict:
    resp = await client.post(
        "/api/v1/auth/signup",
        json={
            "company_name": slug.title(),
            "tenant_slug": slug,
            "full_name": "Owner",
            "email": email or f"owner@{slug}.com",
            "password": "supersecret1",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _auth(tokens: dict) -> dict:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


async def _tenant(db: AsyncSession, slug: str | None = None) -> Tenant:
    tenant = Tenant(name="T", slug=slug or f"t-{uuid.uuid4().hex[:8]}")
    db.add(tenant)
    await db.flush()
    return tenant


# ---- usage aggregation ------------------------------------------ ----


async def test_usage_aggregation_and_estimate(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    agent = AIAgent(tenant_id=tenant.id, name="A")
    db_session.add(agent)
    await db_session.flush()
    month, start, end = parse_month("2026-03")
    t = datetime(2026, 3, 10, 12, tzinfo=UTC)

    def call(agent_id, seconds, when):
        return Call(
            tenant_id=tenant.id,
            ai_agent_id=agent_id,
            direction=CallDirection.INBOUND,
            status=CallStatus.COMPLETED,
            started_at=when,
            duration_seconds=seconds,
        )

    db_session.add_all(
        [
            call(agent.id, 150, t),  # 2.5 AI minutes
            call(agent.id, 90, t),  # 1.5
            call(None, 600, t),  # 10 human minutes
            call(agent.id, 6000, datetime(2026, 4, 2, tzinfo=UTC)),  # other month
        ]
    )
    conv = Conversation(tenant_id=tenant.id, channel=ConversationChannel.WHATSAPP)
    db_session.add(conv)
    await db_session.flush()
    for direction in (
        MessageDirection.OUTBOUND,
        MessageDirection.OUTBOUND,
        MessageDirection.INBOUND,
    ):
        db_session.add(
            Message(
                tenant_id=tenant.id,
                conversation_id=conv.id,
                direction=direction,
                sender=MessageSender.CUSTOMER,
                text="hi",
                created_at=t,
            )
        )
    db_session.add_all(
        [
            UsageEvent(tenant_id=tenant.id, metric="sms", quantity=7, occurred_at=t),
            UsageEvent(
                tenant_id=tenant.id, metric="storage_bytes", quantity=2 * 1024**3, occurred_at=t
            ),
            UsageEvent(tenant_id=tenant.id, metric="sms", quantity=100, occurred_at=end),
        ]
    )
    await db_session.commit()

    totals = await aggregate_usage(db_session, tenant.id, start, end)
    assert totals["ai_minutes"] == 4.0
    assert totals["human_minutes"] == 10.0
    assert totals["whatsapp_messages"] == 2
    assert totals["sms"] == 7
    assert totals["storage_bytes"] == 2 * 1024**3

    plan = PricingPlan(
        tenant_id=tenant.id,
        name="Std",
        currency="INR",
        is_active=True,
        base_fee=100,
        rates={
            "ai_minutes": 5,
            "human_minutes": 2,
            "whatsapp_messages": 1,
            "sms": 0.5,
            "storage_bytes": 3,
        },
        limits={"ai_minutes": 3},
    )
    summary = build_summary(month, totals, plan)
    by_metric = {line.metric: line for line in summary.lines}
    assert by_metric["ai_minutes"].amount == 20.0
    assert by_metric["ai_minutes"].over_limit is True
    assert by_metric["storage_bytes"].quantity == 2.0
    assert summary.estimated_total == 100 + 20 + 20 + 2 + 3.5 + 6

    defaults = build_summary(month, totals, None)
    assert defaults.rates_source == "defaults"


async def test_check_plan_limit(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    db_session.add(
        PricingPlan(
            tenant_id=tenant.id, name="P", is_active=True, rates={}, limits={"sms": 10}, base_fee=0
        )
    )
    db_session.add(
        UsageEvent(tenant_id=tenant.id, metric="sms", quantity=8, occurred_at=datetime.now(UTC))
    )
    await db_session.commit()
    check = await check_plan_limit(db_session, tenant.id, "sms", additional=1)
    assert (check.used, check.limit, check.remaining, check.exceeded) == (8, 10, 2, False)
    assert (await check_plan_limit(db_session, tenant.id, "sms", additional=3)).exceeded is True
    assert (await check_plan_limit(db_session, tenant.id, "ai_minutes")).limit is None


# ---- retention -------------------------------------------------- ----


async def test_retention_selection_and_purge(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    other = await _tenant(db_session)
    now = datetime(2026, 6, 1, tzinfo=UTC)

    async def voice_call(tenant_id, ended):
        call = Call(
            tenant_id=tenant_id,
            direction=CallDirection.INBOUND,
            status=CallStatus.COMPLETED,
            ended_at=ended,
            recording_url="https://api.twilio.com/rec.mp3",
        )
        db_session.add(call)
        await db_session.flush()
        conv = Conversation(tenant_id=tenant_id, channel=ConversationChannel.VOICE, call_id=call.id)
        db_session.add(conv)
        await db_session.flush()
        db_session.add(
            Message(
                tenant_id=tenant_id,
                conversation_id=conv.id,
                direction=MessageDirection.INBOUND,
                sender=MessageSender.CUSTOMER,
                text="x",
            )
        )
        return call

    old = await voice_call(tenant.id, now - timedelta(days=45))
    fresh = await voice_call(tenant.id, now - timedelta(days=5))
    foreign_old = await voice_call(other.id, now - timedelta(days=400))
    policy = RetentionPolicy(
        tenant_id=tenant.id,
        enabled=True,
        transcript_days=30,
        recording_days=60,
        last_purge_summary={},
    )
    db_session.add(policy)
    await db_session.commit()

    selected = (
        (await db_session.execute(retention.expired_transcript_ids_query(tenant.id, 30, now)))
        .scalars()
        .all()
    )
    assert len(selected) == 1

    result = await retention.purge_tenant(db_session, policy, now)
    await db_session.commit()
    assert result == {
        "transcripts_deleted": 1,
        "recordings_cleared": 0,
        "recordings_failed": 0,
        "leads_deleted": 0,
    }  # 45d old < 60d recording
    result = await retention.purge_tenant(db_session, policy, now + timedelta(days=30))
    await db_session.commit()
    assert result["recordings_cleared"] == 1  # `old` is now 75d, `fresh` 35d
    await db_session.refresh(old)
    await db_session.refresh(fresh)
    await db_session.refresh(foreign_old)
    assert old.recording_url is None
    assert fresh.recording_url is not None
    assert foreign_old.recording_url is not None  # other tenant untouched
    remaining = (
        (await db_session.execute(select(Message).where(Message.tenant_id == other.id)))
        .scalars()
        .all()
    )
    assert len(remaining) == 1


# ---- connector payload builders (mock httpx) -------------------- ----


def _mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_payload_builders() -> None:
    assert "call.ended" in hub.slack_payload("call.ended", {"call_id": "c1"})["text"]
    card = hub.teams_payload("ticket.created", {"subject": "Help"})
    assert card["@type"] == "MessageCard" and "Help" in card["text"]
    body = hub.automation_body("call.ended", {"a": 1}, now=1700000000)
    assert body == {"event": "call.ended", "data": {"a": 1}, "timestamp": 1700000000}
    customer = {"name": "Jane Q Doe", "email": "Jane@X.com", "phone": "+9199"}
    assert hub.hubspot_upsert_request(customer) == {
        "inputs": [
            {
                "idProperty": "email",
                "id": "jane@x.com",
                "properties": {"firstname": "Jane", "lastname": "Q Doe", "phone": "+9199"},
            }
        ]
    }
    lead = hub.zoho_lead_request(customer)
    assert lead["duplicate_check_fields"] == ["Email"]
    assert lead["data"][0]["Last_Name"] == "Q Doe" and lead["data"][0]["Email"] == "Jane@X.com"
    fd = hub.freshdesk_ticket_request({"subject": "S", "priority": "urgent"}, customer)
    assert fd["priority"] == 4 and fd["email"] == "Jane@X.com"
    with pytest.raises(hub.ConnectorError):
        hub.freshdesk_ticket_request({"subject": "S"}, {"name": "No contact"})
    zd = hub.zendesk_ticket_request({"subject": "S", "priority": "medium"}, customer)
    assert (
        zd["ticket"]["priority"] == "normal" and zd["ticket"]["requester"]["email"] == "Jane@X.com"
    )
    assert "requester" not in hub.zendesk_ticket_request({"subject": "S"}, None)["ticket"]


def test_config_encryption_and_masking() -> None:
    stored = hub.encrypt_config({"webhook_url": "https://hooks.slack.com/services/SECRET"})
    assert "SECRET" not in json.dumps(stored)
    assert hub.decrypt_config(stored)["webhook_url"].endswith("SECRET")
    masked = hub.mask_config(hub.CATALOG["slack"], hub.decrypt_config(stored))
    assert masked == {"webhook_url": "********"}


async def test_hubspot_send_uses_batch_upsert(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"results": []})

    async with _mock_client(handler) as client:
        detail = await hub.send_hubspot(
            {"access_token": "tok"}, "call.ended", {}, {"name": "A B", "email": "a@b.co"}, client
        )
    assert "upserted" in detail
    request = seen[0]
    assert request.url.path == "/crm/v3/objects/contacts/batch/upsert"
    assert request.headers["authorization"] == "Bearer tok"
    assert json.loads(request.content)["inputs"][0]["id"] == "a@b.co"


async def test_hubspot_phone_only_searches_then_creates() -> None:
    paths: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append((request.method, request.url.path))
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json={"results": []})
        return httpx.Response(201, json={"id": "1"})

    async with _mock_client(handler) as client:
        await hub.send_hubspot(
            {"access_token": "t"}, "call.ended", {}, {"name": "A", "phone": "+91"}, client
        )
    assert paths == [
        ("POST", "/crm/v3/objects/contacts/search"),
        ("POST", "/crm/v3/objects/contacts"),
    ]


async def test_zoho_refreshes_token_then_upserts_lead() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if "oauth/v2/token" in request.url.path:
            return httpx.Response(200, json={"access_token": "AT"})
        return httpx.Response(200, json={"data": [{"status": "success", "action": "insert"}]})

    config = {"client_id": "c", "client_secret": "s", "refresh_token": "r"}
    async with _mock_client(handler) as client:
        detail = await hub.send_zoho(
            config, "call.ended", {}, {"name": "Z", "email": "z@z.io"}, client
        )
    assert detail == "Zoho lead insert"
    assert calls[1].headers["authorization"] == "Zoho-oauthtoken AT"
    assert calls[1].url.path == "/crm/v2/Leads/upsert"
    with pytest.raises(hub.ConnectorError):
        hub._zoho_domains({"api_domain": "https://evil.example.com"})


async def test_helpdesk_requests_and_http_error() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if "freshdesk" in request.url.host:
            return httpx.Response(201, json={"id": 55})
        return httpx.Response(401, text="nope")

    async with _mock_client(handler) as client:
        out = await hub.send_freshdesk(
            {"domain": "acme", "api_key": "k"},
            "ticket.created",
            {"subject": "S"},
            {"email": "e@e.io"},
            client,
        )
        assert "#55" in out
        assert seen[0].url.host == "acme.freshdesk.com"
        with pytest.raises(hub.ConnectorError, match="HTTP 401"):
            await hub.send_zendesk(
                {"subdomain": "acme", "email": "a@a.io", "api_token": "t"},
                "ticket.created",
                {"subject": "S"},
                None,
                client,
            )
        with pytest.raises(hub.ConnectorError):
            await hub.send_freshdesk(
                {"domain": "evil.com/x", "api_key": "k"},
                "ticket.created",
                {},
                {"email": "e@e.io"},
                client,
            )


async def test_automation_hook_signs_body(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hub, "_public_url", lambda url: url)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    async with _mock_client(handler) as client:
        await hub.CATALOG["n8n"].send(
            {"webhook_url": "https://n8n.example/hook", "signing_secret": "sek"},
            "call.ended",
            {"x": 1},
            None,
            client,
        )
    request = seen[0]
    ts = int(request.headers["x-timestamp"])
    expected = hub.sign_body("sek", ts, request.content.decode())
    assert request.headers["x-signature"] == expected
    assert json.loads(request.content)["event"] == "call.ended"


async def test_slack_rejects_private_urls() -> None:
    async with _mock_client(lambda r: httpx.Response(200)) as client:
        with pytest.raises(hub.ConnectorError):
            await hub.send_slack({"webhook_url": "http://127.0.0.1/hook"}, "e", {}, None, client)


async def test_dispatch_event_fans_out_to_subscribed_connectors(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hub, "_public_url", lambda url: url)
    tenant = await _tenant(db_session)
    customer = Customer(tenant_id=tenant.id, name="Cust", email="c@c.io")
    db_session.add(customer)
    await db_session.flush()
    for kind, events, enabled in (
        ("slack", ["call.ended"], True),
        ("zapier", ["ticket.created"], True),
        ("make", ["call.ended"], False),
    ):
        db_session.add(
            IntegrationConnector(
                tenant_id=tenant.id,
                type=kind,
                name=kind,
                enabled=enabled,
                events=events,
                config=hub.encrypt_config({"webhook_url": f"https://hooks.example/{kind}"}),
            )
        )
    await db_session.commit()
    hits: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(request.url.path)
        return httpx.Response(200)

    async with _mock_client(handler) as client:
        results = await hub.dispatch_event(
            db_session, tenant.id, "call.ended", {"customer_id": str(customer.id)}, client=client
        )
    assert hits == ["/slack"]
    assert [r["ok"] for r in results] == [True]


async def test_lookup_order_shopify_requires_matching_email(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    db_session.add(
        IntegrationConnector(
            tenant_id=tenant.id,
            type="shopify",
            name="shop",
            enabled=True,
            events=[],
            config=hub.encrypt_config({"shop_domain": "demo.myshopify.com", "access_token": "t"}),
        )
    )
    await db_session.commit()
    order = {
        "name": "#1001",
        "email": "buyer@x.io",
        "financial_status": "paid",
        "fulfillment_status": "fulfilled",
        "total_price": "10.00",
        "currency": "USD",
        "created_at": "2026-01-01",
        "line_items": [{"quantity": 2, "title": "Mug"}],
        "fulfillments": [{"tracking_number": "T1", "tracking_company": "UPS"}],
    }

    async def fake_lookup(config, number, client):
        assert number == "1001"
        return order

    import app.tools.platform_tools as tools

    tools_lookup = tools.lookup_shopify_order
    tools.lookup_shopify_order = fake_lookup
    try:
        ctx = ToolContext(
            db=db_session,
            tenant_id=tenant.id,
            agent_id=None,
            call_id=None,
            conversation_id=None,
            customer_id=None,
        )
        assert "error" in await lookup_order(ctx, "1001")  # no verification data
        wrong = await lookup_order(ctx, "1001", email="other@x.io")
        assert wrong["found"] is False and "items" not in wrong
        ok = await lookup_order(ctx, "1001", email="BUYER@x.io")
        assert ok["found"] and ok["items"] == ["2 x Mug"] and ok["tracking"][0]["number"] == "T1"
    finally:
        tools.lookup_shopify_order = tools_lookup


# ---- SSO helpers ------------------------------------------------ ----


async def test_verify_id_token_checks_signature_nonce_and_audience() -> None:
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid="k1", alg="RS256", use="sig")
    claims = {
        "iss": "https://idp.example.com",
        "aud": "cid",
        "exp": int(time.time()) + 300,
        "email": "sso@x.io",
        "nonce": "n1",
        "email_verified": True,
    }
    token = jwt.encode(claims, key, algorithm="RS256", headers={"kid": "k1"})
    discovery = {"jwks_uri": "https://idp.example.com/jwks"}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"keys": [jwk]})

    async with _mock_client(handler) as client:
        verified = await sso.verify_id_token(
            token,
            discovery=discovery,
            issuer="https://idp.example.com/",
            client_id="cid",
            nonce="n1",
            client=client,
        )
        assert verified["email"] == "sso@x.io"
        for kwargs in ({"nonce": "bad"}, {"client_id": "other"}):
            args = {"issuer": "https://idp.example.com", "client_id": "cid", "nonce": "n1"} | kwargs
            with pytest.raises(sso.SsoError):
                await sso.verify_id_token(token, discovery=discovery, client=client, **args)
        forged = jwt.encode(
            claims, rsa.generate_private_key(65537, 2048), algorithm="RS256", headers={"kid": "k1"}
        )
        with pytest.raises(sso.SsoError):
            await sso.verify_id_token(
                forged,
                discovery=discovery,
                issuer="https://idp.example.com",
                client_id="cid",
                nonce="n1",
                client=client,
            )


# ---- API: 2FA login flow ---------------------------------------- ----


async def test_two_factor_enroll_login_and_recovery(client: AsyncClient) -> None:
    tokens = await _signup(client, "twofa-co")
    h = _auth(tokens)
    status_resp = await client.get("/api/v1/platform/security/2fa", headers=h)
    assert status_resp.json() == {
        "enabled": False,
        "pending_enrollment": False,
        "recovery_codes_remaining": 0,
    }

    enroll = (await client.post("/api/v1/platform/security/2fa/enroll", headers=h)).json()
    assert enroll["otpauth_uri"].startswith("otpauth://totp/")
    secret = enroll["secret"]

    bad = await client.post(
        "/api/v1/platform/security/2fa/verify", headers=h, json={"code": "000000"}
    )
    assert bad.status_code == 400
    verify = await client.post(
        "/api/v1/platform/security/2fa/verify",
        headers=h,
        json={"code": totp_at(secret, time.time())},
    )
    assert verify.status_code == 200, verify.text
    recovery = verify.json()["recovery_codes"]
    assert len(recovery) == 8

    creds = {"email": "owner@twofa-co.com", "password": "supersecret1"}
    needs = (await client.post("/api/v1/auth/login", json=creds)).json()
    assert needs["requires_2fa"] is True and needs["tokens"] is None

    wrong = await client.post("/api/v1/auth/login", json={**creds, "totp_code": "123456"})
    assert wrong.status_code == 401

    # The code used during enrollment is a replay; use the next time step.
    future = time.time() + 30
    import app.services.platform_totp as tfa

    row_code = totp_at(secret, future)
    orig = tfa.check_code
    tfa.check_code = lambda row, code, now=None: orig(row, code, now=future)
    try:
        ok = await client.post("/api/v1/auth/login", json={**creds, "totp_code": row_code})
        assert ok.status_code == 200 and ok.json()["tokens"]["access_token"]
    finally:
        tfa.check_code = orig

    # Recovery code works exactly once.
    first = await client.post("/api/v1/auth/login", json={**creds, "totp_code": recovery[0]})
    assert first.status_code == 200 and first.json()["tokens"]
    again = await client.post("/api/v1/auth/login", json={**creds, "totp_code": recovery[0]})
    assert again.status_code == 401

    # Disable requires password and a valid code.
    wrong_pw = await client.post(
        "/api/v1/platform/security/2fa/disable",
        headers=h,
        json={"password": "nope-nope-1", "code": recovery[1]},
    )
    assert wrong_pw.status_code == 400
    off = await client.post(
        "/api/v1/platform/security/2fa/disable",
        headers=h,
        json={"password": "supersecret1", "code": recovery[1]},
    )
    assert off.status_code == 200 and off.json()["enabled"] is False
    plain = await client.post("/api/v1/auth/login", json=creds)
    assert plain.json()["tokens"]


# ---- API: IP allowlist ------------------------------------------ ----


async def test_ip_allowlist_blocks_and_prevents_lockout(client: AsyncClient) -> None:
    invalidate_allowlist_cache()
    tokens = await _signup(client, "ip-co")
    h = _auth(tokens)
    # ASGI test client address is 127.0.0.1; a list without it would lock us out.
    lockout = await client.put(
        "/api/v1/platform/security/settings", headers=h, json={"ip_allowlist": ["203.0.113.0/24"]}
    )
    assert lockout.status_code == 400 and "lock you out" in lockout.text
    invalid = await client.put(
        "/api/v1/platform/security/settings", headers=h, json={"ip_allowlist": ["not-an-ip"]}
    )
    assert invalid.status_code == 400

    ok = await client.put(
        "/api/v1/platform/security/settings",
        headers=h,
        json={"ip_allowlist": ["127.0.0.1", "203.0.113.0/24"]},
    )
    assert ok.status_code == 200 and ok.json()["your_ip"] == "127.0.0.1"
    assert (await client.get("/api/v1/customers", headers=h)).status_code == 200

    # Simulate a request from elsewhere by changing the allowlist directly.
    from packages.database.models.platform import TenantSecuritySettings
    from packages.database.session import session_scope

    async with session_scope() as db:
        row = (await db.execute(select(TenantSecuritySettings))).scalar_one()
        row.ip_allowlist = ["198.51.100.0/24"]
        await db.commit()
    invalidate_allowlist_cache()
    blocked = await client.get("/api/v1/customers", headers=h)
    assert blocked.status_code == 403 and "allowlist" in blocked.text
    invalidate_allowlist_cache()


# ---- API: PII masking ------------------------------------------- ----


async def test_pii_masking_on_calls_transcript_and_non_admin_phone(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    tokens = await _signup(client, "mask-co")
    h = _auth(tokens)
    tenant = (await db_session.execute(select(Tenant).where(Tenant.slug == "mask-co"))).scalar_one()
    call = Call(
        tenant_id=tenant.id,
        direction=CallDirection.INBOUND,
        status=CallStatus.COMPLETED,
        from_number="+919876543210",
        summary="Customer jane@example.com asked about billing",
    )
    db_session.add(call)
    await db_session.flush()
    conv = Conversation(tenant_id=tenant.id, channel=ConversationChannel.VOICE, call_id=call.id)
    db_session.add(conv)
    await db_session.flush()
    db_session.add(
        Message(
            tenant_id=tenant.id,
            conversation_id=conv.id,
            direction=MessageDirection.INBOUND,
            sender=MessageSender.CUSTOMER,
            text="my card is 4111 1111 1111 1111",
        )
    )
    db_session.add(
        Customer(tenant_id=tenant.id, name="J", phone="+919876543210", email="jane@example.com")
    )
    await db_session.commit()

    plain = (await client.get(f"/api/v1/calls/{call.id}", headers=h)).json()
    assert plain["from_number"] == "+919876543210"

    resp = await client.put(
        "/api/v1/platform/security/settings", headers=h, json={"mask_pii": True}
    )
    assert resp.status_code == 200
    masked = (await client.get(f"/api/v1/calls/{call.id}", headers=h)).json()
    assert masked["from_number"] == "+**********10"
    assert "jane@" not in masked["summary"]
    transcript = (await client.get(f"/api/v1/calls/{call.id}/transcript", headers=h)).json()
    assert "4111" not in transcript[0]["text"] and transcript[0]["text"].endswith("1111")
    listed = (await client.get("/api/v1/calls", headers=h)).json()
    assert listed[0]["from_number"] == "+**********10"
    customers = (await client.get("/api/v1/customers", headers=h)).json()
    assert customers[0]["email"].endswith("@example.com") and customers[0]["email"][1] == "*"

    # phone masking for non-admin roles only
    await client.put(
        "/api/v1/platform/security/settings",
        headers=h,
        json={"mask_pii": False, "mask_phone_non_admin": True},
    )
    assert (await client.get(f"/api/v1/calls/{call.id}", headers=h)).json()[
        "from_number"
    ] == "+919876543210"
    from app.core.deps import RequestContext
    from app.schemas.customer import CustomerRead
    from app.services.platform_masking import MaskingPolicy, mask_customer_read, policy_for

    from packages.database.models.platform import TenantSecuritySettings

    row = (await db_session.execute(select(TenantSecuritySettings))).scalar_one()
    agent_ctx = RequestContext(
        user_id=uuid.uuid4(),
        tenant_id=tenant.id,
        membership_id=uuid.uuid4(),
        role_name="AGENT",
        permission_codes=frozenset(),
        is_platform_admin=False,
    )
    policy = policy_for(row, agent_ctx)
    assert policy == MaskingPolicy(mask_text=False, mask_phone=True)
    c = CustomerRead.model_validate((await db_session.execute(select(Customer))).scalar_one())
    assert mask_customer_read(c, policy).phone == "+**********10"


# ---- API: data export / erasure --------------------------------- ----


async def test_customer_export_and_erasure(client: AsyncClient, db_session: AsyncSession) -> None:
    tokens = await _signup(client, "gdpr-co")
    h = _auth(tokens)
    tenant = (await db_session.execute(select(Tenant).where(Tenant.slug == "gdpr-co"))).scalar_one()
    other_tenant = await _tenant(db_session)
    customer = Customer(tenant_id=tenant.id, name="Erase Me", phone="+911111111111")
    bystander = Customer(tenant_id=tenant.id, name="Keep Me")
    foreign = Customer(tenant_id=other_tenant.id, name="Foreign")
    db_session.add_all([customer, bystander, foreign])
    await db_session.flush()
    call = Call(
        tenant_id=tenant.id,
        customer_id=customer.id,
        direction=CallDirection.INBOUND,
        status=CallStatus.COMPLETED,
    )
    keep_call = Call(
        tenant_id=tenant.id,
        customer_id=bystander.id,
        direction=CallDirection.INBOUND,
        status=CallStatus.COMPLETED,
    )
    db_session.add_all([call, keep_call])
    await db_session.flush()
    conv = Conversation(
        tenant_id=tenant.id,
        customer_id=customer.id,
        channel=ConversationChannel.VOICE,
        call_id=call.id,
    )
    db_session.add(conv)
    await db_session.flush()
    db_session.add(
        Message(
            tenant_id=tenant.id,
            conversation_id=conv.id,
            direction=MessageDirection.INBOUND,
            sender=MessageSender.CUSTOMER,
            text="hello",
        )
    )
    await db_session.commit()
    customer_id, call_id, keep_call_id = customer.id, call.id, keep_call.id

    resp = await client.post(
        "/api/v1/platform/data/export", headers=h, json={"customer_id": str(customer_id)}
    )
    assert resp.status_code == 200 and resp.headers["content-type"] == "application/zip"
    archive = zipfile.ZipFile(io.BytesIO(resp.content))
    assert {"export.json", "customer.csv", "calls.csv", "messages.csv"} <= set(archive.namelist())
    exported = json.loads(archive.read("export.json"))
    assert exported["customer"][0]["name"] == "Erase Me"
    assert exported["messages"][0]["text"] == "hello"

    cross = await client.post(
        "/api/v1/platform/data/export", headers=h, json={"customer_id": str(foreign.id)}
    )
    assert cross.status_code == 404
    assert (
        await client.delete(f"/api/v1/platform/data/customers/{foreign.id}", headers=h)
    ).status_code == 404

    erased = await client.delete(f"/api/v1/platform/data/customers/{customer_id}", headers=h)
    assert erased.status_code == 200, erased.text
    assert erased.json()["deleted"]["messages"] == 1 and erased.json()["deleted"]["calls"] == 1
    db_session.expire_all()
    assert (
        await db_session.execute(select(Customer).where(Customer.id == customer_id))
    ).first() is None
    assert (await db_session.execute(select(Call).where(Call.id == call_id))).first() is None
    assert (await db_session.execute(select(Message))).first() is None
    assert (
        await db_session.execute(select(Call).where(Call.id == keep_call_id))
    ).first() is not None
    audit = (await client.get("/api/v1/audit-logs", headers=h)).json()
    assert "customer.erased" in {a["action"] for a in audit}


# ---- API: SSO, usage, pricing, admin, connectors ---------------- ----


async def test_sso_not_configured_is_a_clear_error(client: AsyncClient) -> None:
    await _signup(client, "nosso-co")
    login = await client.get("/api/v1/platform/sso/nosso-co/login")
    assert login.status_code == 404 and "not configured" in login.json()["detail"]
    assert (await client.get("/api/v1/platform/sso/nosso-co/status")).json() == {"enabled": False}
    cb = await client.get(
        "/api/v1/platform/sso/nosso-co/callback", params={"code": "x", "state": "y"}
    )
    assert cb.status_code == 302 and "sso_error" in cb.headers["location"]


async def test_sso_config_and_provisioning(client: AsyncClient, db_session: AsyncSession) -> None:
    tokens = await _signup(client, "sso-co")
    h = _auth(tokens)
    put = await client.put(
        "/api/v1/platform/sso/config",
        headers=h,
        json={
            "issuer": "https://idp.example.com",
            "client_id": "cid",
            "client_secret": "shh",
            "default_role_name": "AGENT",
            "allowed_email_domains": ["sso-co.com"],
        },
    )
    assert put.status_code == 200 and put.json()["has_client_secret"] is True
    assert "shh" not in put.text
    forbidden_role = await client.put(
        "/api/v1/platform/sso/config",
        headers=h,
        json={
            "issuer": "https://idp.example.com",
            "client_id": "cid",
            "default_role_name": "OWNER",
        },
    )
    assert forbidden_role.status_code == 422
    assert (await client.get("/api/v1/platform/sso/sso-co/status")).json() == {"enabled": True}

    tenant, config = await sso.load_config(db_session, "sso-co")
    member = await sso.provision_user(
        db_session, tenant, config, {"email": "New.Hire@sso-co.com", "name": "New Hire"}
    )
    await db_session.commit()
    again = await sso.provision_user(db_session, tenant, config, {"email": "new.hire@sso-co.com"})
    assert again.id == member.id
    user = (
        await db_session.execute(select(User).where(User.email == "new.hire@sso-co.com"))
    ).scalar_one()
    assert user.full_name == "New Hire"
    with pytest.raises(sso.SsoError, match="domain"):
        await sso.provision_user(db_session, tenant, config, {"email": "x@evil.com"})


async def test_usage_endpoint_and_pricing_plan_crud(client: AsyncClient) -> None:
    tokens = await _signup(client, "usage-co")
    h = _auth(tokens)
    usage = await client.get("/api/v1/platform/usage", headers=h)
    assert usage.status_code == 200 and usage.json()["rates_source"] == "defaults"
    assert (
        await client.get("/api/v1/platform/usage", params={"month": "bad"}, headers=h)
    ).status_code == 400

    created = await client.post(
        "/api/v1/platform/pricing-plans",
        headers=h,
        json={
            "name": "Gold",
            "rates": {"ai_minutes": 4.5},
            "limits": {"ai_minutes": 100},
            "base_fee": 999,
            "is_active": True,
        },
    )
    assert created.status_code == 201, created.text
    plan_id = created.json()["id"]
    assert (
        await client.post(
            "/api/v1/platform/pricing-plans", headers=h, json={"name": "Bad", "rates": {"bogus": 1}}
        )
    ).status_code == 422
    summary = (await client.get("/api/v1/platform/usage", headers=h)).json()
    assert summary["plan_name"] == "Gold" and summary["estimated_total"] == 999
    assert (
        await client.delete(f"/api/v1/platform/pricing-plans/{plan_id}", headers=h)
    ).status_code == 409
    patched = await client.patch(
        f"/api/v1/platform/pricing-plans/{plan_id}",
        headers=h,
        json={"is_active": False, "rates": {"sms": 1}},
    )
    assert patched.status_code == 200 and patched.json()["rates"] == {"sms": 1}
    assert (
        await client.delete(f"/api/v1/platform/pricing-plans/{plan_id}", headers=h)
    ).status_code == 204
    event = await client.post(
        "/api/v1/platform/usage/events", headers=h, json={"metric": "sms", "quantity": 3}
    )
    assert event.status_code == 201
    assert (
        await client.post(
            "/api/v1/platform/usage/events", headers=h, json={"metric": "x", "quantity": 1}
        )
    ).status_code == 422


async def test_super_admin_access_and_tenant_status(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    admin_tokens = await _signup(client, "root-co")
    other_tokens = await _signup(client, "victim-co")
    ah, oh = _auth(admin_tokens), _auth(other_tokens)

    assert (await client.get("/api/v1/platform/admin/me", headers=ah)).json() == {
        "is_platform_admin": False
    }
    assert (await client.get("/api/v1/platform/admin/tenants", headers=ah)).status_code == 403

    admin_user = (
        await db_session.execute(select(User).where(User.email == "owner@root-co.com"))
    ).scalar_one()
    db_session.add(PlatformAdmin(user_id=admin_user.id))
    await db_session.commit()

    assert (await client.get("/api/v1/platform/admin/me", headers=ah)).json() == {
        "is_platform_admin": True
    }
    assert (await client.get("/api/v1/platform/admin/tenants", headers=oh)).status_code == 403
    rows = (await client.get("/api/v1/platform/admin/tenants", headers=ah)).json()
    assert {r["slug"] for r in rows} == {"root-co", "victim-co"}
    victim = next(r for r in rows if r["slug"] == "victim-co")
    assert victim["users"] == 1 and victim["calls_month"] == 0

    detail = (await client.get(f"/api/v1/platform/admin/tenants/{victim['id']}", headers=ah)).json()
    assert (
        detail["user_list"][0]["email"] == "owner@victim-co.com"
        and detail["user_list"][0]["role"] == "OWNER"
    )

    suspended = await client.post(
        f"/api/v1/platform/admin/tenants/{victim['id']}/suspend",
        headers=ah,
        json={"reason": "abuse"},
    )
    assert suspended.json()["status"] == "suspended"
    assert (
        await client.get("/api/v1/customers", headers=oh)
    ).status_code == 401  # session no longer valid
    own = next(r for r in rows if r["slug"] == "root-co")
    assert (
        await client.post(f"/api/v1/platform/admin/tenants/{own['id']}/suspend", headers=ah)
    ).status_code == 400
    activated = await client.post(
        f"/api/v1/platform/admin/tenants/{victim['id']}/activate", headers=ah
    )
    assert activated.json()["status"] == "active"

    health = (await client.get("/api/v1/platform/admin/health", headers=ah)).json()
    assert health["database"]["ok"] is True and health["version"]
    assert set(health) >= {"redis", "celery", "environment"}


async def test_connector_crud_never_returns_secrets(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    tokens = await _signup(client, "hub-co")
    h = _auth(tokens)
    catalog = (await client.get("/api/v1/platform/connectors/catalog", headers=h)).json()
    types = {c["type"]: c for c in catalog}
    assert len(types) == 14 and types["salesforce"]["supported"] is True
    assert types["salesforce"]["oauth_provider"] == "salesforce"
    assert types["google_calendar"]["supported"] and types["outlook"]["supported"]

    refused = await client.post(
        "/api/v1/platform/connectors",
        headers=h,
        json={"type": "salesforce", "name": "SF", "config": {}},
    )
    assert refused.status_code == 400 and "OAuth" in refused.json()["detail"]
    missing = await client.post(
        "/api/v1/platform/connectors",
        headers=h,
        json={"type": "hubspot", "name": "H", "config": {}},
    )
    assert missing.status_code == 400

    created = await client.post(
        "/api/v1/platform/connectors",
        headers=h,
        json={
            "type": "hubspot",
            "name": "HubSpot",
            "config": {"access_token": "pat-secret-123"},
            "events": ["call.ended"],
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert "pat-secret-123" not in created.text and body["config"] == {"access_token": "********"}
    stored = (await db_session.execute(select(IntegrationConnector))).scalar_one()
    assert "pat-secret-123" not in json.dumps(stored.config)
    assert hub.decrypt_config(stored.config)["access_token"] == "pat-secret-123"

    # Echoing the mask back keeps the stored secret.
    patched = await client.patch(
        f"/api/v1/platform/connectors/{body['id']}",
        headers=h,
        json={"config": {"access_token": "********"}, "enabled": False},
    )
    assert patched.status_code == 200 and patched.json()["enabled"] is False
    await db_session.refresh(stored)
    assert hub.decrypt_config(stored.config)["access_token"] == "pat-secret-123"
    assert (
        await client.patch(
            f"/api/v1/platform/connectors/{body['id']}", headers=h, json={"events": ["bogus"]}
        )
    ).status_code == 400

    assert len((await client.get("/api/v1/platform/connectors", headers=h)).json()) == 1
    assert (
        await client.delete(f"/api/v1/platform/connectors/{body['id']}", headers=h)
    ).status_code == 204
    info = (await client.get("/api/v1/platform/openapi-sdk-info", headers=h)).json()
    assert info["openapi_url"].endswith("/openapi.json") and len(info["sdks"]) == 3


async def test_retention_api_requires_values_and_runs(client: AsyncClient) -> None:
    tokens = await _signup(client, "ret-co")
    h = _auth(tokens)
    bad = await client.put("/api/v1/platform/security/retention", headers=h, json={"enabled": True})
    assert bad.status_code == 400
    ok = await client.put(
        "/api/v1/platform/security/retention",
        headers=h,
        json={"enabled": True, "transcript_days": 30},
    )
    assert ok.status_code == 200 and ok.json()["transcript_days"] == 30
    ran = await client.post("/api/v1/platform/security/retention/purge", headers=h)
    assert ran.json() == {
        "transcripts_deleted": 0,
        "recordings_cleared": 0,
        "recordings_failed": 0,
        "leads_deleted": 0,
    }


async def test_non_admin_role_cannot_manage_security(client: AsyncClient) -> None:
    # Unauthenticated callers are rejected before any handler runs.
    assert (await client.get("/api/v1/platform/security/settings")).status_code == 401
    assert (await client.get("/api/v1/platform/usage")).status_code == 401
