"""Exotel, the Indian carrier: connecting a workspace, and accepting the calls it streams to us."""

import uuid

import httpx
import pytest
from app.core.config import get_settings
from app.services import exotel
from httpx import AsyncClient
from sqlalchemy import select

from packages.database.models import BlockedCaller, Call, Integration
from packages.database.session import session_scope
from tests.test_whatsapp_pipeline import _auth, _signup

CREDS = {
    "account_sid": "acme1",
    "api_key": "key-abc",
    "api_token": "token-xyz",
    "subdomain": "api.exotel.com",
}


def _internal() -> dict:
    return {"X-Internal-Token": get_settings().internal_service_token}


@pytest.fixture(autouse=True)
def _exotel_accepts_details(monkeypatch):
    async def ok(**_kw):
        return None

    monkeypatch.setattr(exotel, "check_credentials", ok)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("09876543210", "+919876543210"),
        ("9876543210", "+919876543210"),
        ("+919876543210", "+919876543210"),
        ("919876543210", "+919876543210"),
        ("00919876543210", "+919876543210"),
        ("08071234567", "+918071234567"),  # a Bengaluru landline
        (" +91 98765 43210 ", "+919876543210"),
        ("", ""),
    ],
)
def test_numbers_are_stored_in_one_format(raw, expected):
    assert exotel.to_e164(raw) == expected


async def _workspace(client: AsyncClient, slug: str, number: str = "+918000000002", creds=None):
    tokens = await _signup(client, slug)
    agent = await client.post("/api/v1/ai-agents", headers=_auth(tokens), json={"name": "Desk"})
    assert agent.status_code == 201, agent.text
    setup = await client.post(
        "/api/v1/integrations/exotel", headers=_auth(tokens), json=creds or CREDS
    )
    assert setup.status_code == 201, setup.text
    key = setup.json()["stream_url"].split("k=")[1]
    added = await client.post(
        "/api/v1/phone-numbers",
        headers=_auth(tokens),
        json={"e164_number": number, "provider": "exotel", "ai_agent_id": agent.json()["id"]},
    )
    assert added.status_code == 201, added.text
    return tokens, key, agent.json()["id"]


def _start(key: str, **over) -> dict:
    body = {"stream_key": key, "call_sid": "ex-call-1", "from": "09800000001", "to": "08000000002"}
    body.update(over)
    return body


async def test_connecting_gives_a_private_address_and_keeps_the_secret_out_of_the_list(
    client: AsyncClient,
):
    tokens = await _signup(client, "acme")
    resp = await client.post("/api/v1/integrations/exotel", headers=_auth(tokens), json=CREDS)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert "/ws/exotel?k=" in body["stream_url"]
    assert len(body["steps"]) >= 4
    key = body["stream_url"].split("k=")[1]

    listed = await client.get("/api/v1/integrations", headers=_auth(tokens))
    text = listed.text
    assert key not in text and "token-xyz" not in text and "key-abc" not in text

    async with session_scope() as db:
        row = (
            await db.execute(select(Integration).where(Integration.provider == "exotel"))
        ).scalar_one()
        assert "token-xyz" not in str(row.config) and key not in str(row.config)


async def test_wrong_details_say_what_to_fix(client: AsyncClient, monkeypatch):
    async def refuse(**_kw):
        raise exotel.ExotelError("Exotel did not accept this API key and token.")

    monkeypatch.setattr(exotel, "check_credentials", refuse)
    tokens = await _signup(client, "acme")
    resp = await client.post("/api/v1/integrations/exotel", headers=_auth(tokens), json=CREDS)
    assert resp.status_code == 422
    assert "did not accept" in resp.json()["detail"]


async def test_connecting_needs_a_signed_in_user(client: AsyncClient):
    assert (await client.post("/api/v1/integrations/exotel", json=CREDS)).status_code in (401, 403)


async def test_an_exotel_number_needs_exotel_connected_first(client: AsyncClient):
    tokens = await _signup(client, "acme")
    resp = await client.post(
        "/api/v1/phone-numbers",
        headers=_auth(tokens),
        json={"e164_number": "+918000000002", "provider": "exotel"},
    )
    assert resp.status_code == 422
    assert "Exotel" in resp.json()["detail"]


async def test_a_call_to_the_workspaces_number_starts_a_call(client: AsyncClient):
    tokens, key, agent_id = await _workspace(client, "acme")
    resp = await client.post("/api/v1/internal/exotel/start", headers=_internal(), json=_start(key))
    assert resp.status_code == 200, resp.text
    call_id = resp.json()["call_id"]

    async with session_scope() as db:
        call = await db.get(Call, uuid.UUID(call_id))
        assert call.from_number == "+919800000001" and call.to_number == "+918000000002"
        assert str(call.ai_agent_id) == agent_id
        assert call.provider_call_sid == "ex-call-1"

    # The gateway reconnecting for the same call must not create a second one.
    again = await client.post(
        "/api/v1/internal/exotel/start", headers=_internal(), json=_start(key)
    )
    assert again.json()["call_id"] == call_id


async def test_unknown_address_and_unknown_number_are_refused(client: AsyncClient):
    _, key, _ = await _workspace(client, "acme")
    bad_key = await client.post(
        "/api/v1/internal/exotel/start", headers=_internal(), json=_start("not-a-real-key-123")
    )
    assert bad_key.status_code == 403
    other_number = await client.post(
        "/api/v1/internal/exotel/start", headers=_internal(), json=_start(key, to="+919999999999")
    )
    assert other_number.status_code == 404


async def test_one_workspace_cannot_use_anothers_number(client: AsyncClient):
    _, key_a, _ = await _workspace(client, "alpha", number="+918000000002")
    _, key_b, _ = await _workspace(
        client, "beta", number="+918000000003", creds={**CREDS, "account_sid": "beta1"}
    )
    # B's address asked about A's number, and the other way round.
    assert (
        await client.post("/api/v1/internal/exotel/start", headers=_internal(), json=_start(key_b))
    ).status_code == 404
    assert (
        await client.post(
            "/api/v1/internal/exotel/start",
            headers=_internal(),
            json=_start(key_a, to="08000000003"),
        )
    ).status_code == 404
    ok = await client.post(
        "/api/v1/internal/exotel/start", headers=_internal(), json=_start(key_b, to="08000000003")
    )
    assert ok.status_code == 200


async def test_only_the_gateway_may_call_this(client: AsyncClient):
    _, key, _ = await _workspace(client, "acme")
    assert (await client.post("/api/v1/internal/exotel/start", json=_start(key))).status_code in (
        401,
        403,
    )
    assert (
        await client.post(
            "/api/v1/internal/exotel/start", headers={"X-Internal-Token": "wrong"}, json=_start(key)
        )
    ).status_code in (401, 403)


async def test_blocked_callers_are_refused(client: AsyncClient):
    tokens, key, _ = await _workspace(client, "acme")
    me = await client.get("/api/v1/users/me", headers=_auth(tokens))
    async with session_scope() as db:
        db.add(
            BlockedCaller(
                tenant_id=me.json()["tenant_id"], e164_number="+919800000001", reason="test"
            )
        )
        await db.commit()
    resp = await client.post("/api/v1/internal/exotel/start", headers=_internal(), json=_start(key))
    assert resp.status_code == 403


async def test_a_new_stream_address_replaces_the_old_one(client: AsyncClient):
    tokens, old_key, _ = await _workspace(client, "acme")
    fresh = await client.post("/api/v1/integrations/exotel/stream-address", headers=_auth(tokens))
    assert fresh.status_code == 200, fresh.text
    new_key = fresh.json()["stream_url"].split("k=")[1]
    assert new_key != old_key
    assert (
        await client.post(
            "/api/v1/internal/exotel/start", headers=_internal(), json=_start(old_key)
        )
    ).status_code == 403
    assert (
        await client.post(
            "/api/v1/internal/exotel/start", headers=_internal(), json=_start(new_key)
        )
    ).status_code == 200


async def test_disconnecting_switches_the_address_off(client: AsyncClient):
    tokens, key, _ = await _workspace(client, "acme")
    assert (
        await client.delete("/api/v1/integrations/exotel", headers=_auth(tokens))
    ).status_code == 204
    assert (
        await client.post("/api/v1/internal/exotel/start", headers=_internal(), json=_start(key))
    ).status_code == 403


# ---- the check against Exotel itself (its answer is faked) ----------------------------


def _fake_exotel(monkeypatch, status: int):
    real = httpx.AsyncClient

    def factory(*a, **k):
        return real(
            transport=httpx.MockTransport(lambda req: httpx.Response(status, json={})),
            timeout=k.get("timeout"),
        )

    monkeypatch.setattr(exotel.httpx, "AsyncClient", factory)


@pytest.mark.parametrize(
    "status, words",
    [(401, "did not accept"), (404, "does not recognise"), (400, "does not recognise"), (500, r"error \(500\)")],
)
async def test_exotels_answers_become_plain_sentences(monkeypatch, status, words):
    monkeypatch.undo()  # use the real check, not the always-ok one
    _fake_exotel(monkeypatch, status)
    with pytest.raises(exotel.ExotelError, match=words):
        await exotel.check_credentials(
            subdomain="api.exotel.com", account_sid="a", api_key="k", api_token="t"
        )


async def test_good_details_pass_and_a_made_up_region_is_refused(monkeypatch):
    monkeypatch.undo()
    _fake_exotel(monkeypatch, 200)
    await exotel.check_credentials(
        subdomain="api.in.exotel.com", account_sid="a", api_key="k", api_token="t"
    )
    with pytest.raises(exotel.ExotelError, match="region"):
        await exotel.check_credentials(
            subdomain="evil.example.com", account_sid="a", api_key="k", api_token="t"
        )


# ---- outbound calls -------------------------------------------------------------------------


def _exotel_dials(monkeypatch, *, status=200, body=None) -> list[dict]:
    """Stands in for Exotel's Calls/connect; returns the requests it received."""
    sent: list[dict] = []

    async def post(url, data, *, auth):
        sent.append({"url": url, "data": data, "auth": auth})
        return httpx.Response(
            status, json=body if body is not None else {"Call": {"Sid": "ex-out-1"}}
        )

    monkeypatch.setattr(exotel, "_post", post)
    return sent


def _no_twilio(monkeypatch):
    def refuse(**_kw):
        raise AssertionError("an Exotel number must not dial through Twilio")

    monkeypatch.setattr("app.api.v1.calls.place_outbound_call", refuse)


async def _outbound_ready(client: AsyncClient, slug: str):
    tokens, key, agent_id = await _workspace(client, slug)
    resp = await client.put(
        "/api/v1/integrations/exotel/outbound", headers=_auth(tokens), json={"app_id": "1367464"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["config"]["outbound_app_id"] == "1367464"
    return tokens, key, agent_id


async def _dial(client: AsyncClient, tokens, agent_id):
    return await client.post(
        "/api/v1/calls",
        headers=_auth(tokens),
        json={
            "direction": "outbound",
            "to_number": "+919800000001",
            "from_number": "+918000000002",
            "ai_agent_id": agent_id,
        },
    )


async def test_a_call_from_an_exotel_number_goes_out_through_exotel(
    client: AsyncClient, monkeypatch
):
    sent = _exotel_dials(monkeypatch)
    _no_twilio(monkeypatch)
    tokens, _, agent_id = await _outbound_ready(client, "dial-out")

    resp = await _dial(client, tokens, agent_id)
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "ringing"

    (request,) = sent
    assert request["url"] == "https://api.exotel.com/v1/Accounts/acme1/Calls/connect.json"
    assert request["auth"] == ("key-abc", "token-xyz")
    data = request["data"]
    assert data["From"] == "+919800000001"
    assert data["CallerId"] == "08000000002"
    assert data["Url"] == "http://my.exotel.com/acme1/exoml/start_voice/1367464"
    assert f"call_id={resp.json()['id']}" in data["StatusCallback"]
    async with session_scope() as db:
        call = await db.get(Call, uuid.UUID(resp.json()["id"]))
        assert call.provider_call_sid == "ex-out-1"


async def test_without_a_flow_app_id_the_owner_is_told_where_to_add_it(
    client: AsyncClient, monkeypatch
):
    sent = _exotel_dials(monkeypatch)
    _no_twilio(monkeypatch)
    tokens, _, agent_id = await _workspace(client, "no-flow")

    resp = await _dial(client, tokens, agent_id)
    assert resp.status_code == 424
    assert "App ID" in resp.json()["detail"]
    assert sent == []


async def test_exotels_refusal_reaches_the_owner_in_its_own_words(client: AsyncClient, monkeypatch):
    _exotel_dials(
        monkeypatch,
        status=403,
        body={
            "RestException": {
                "Status": 403,
                "Message": "Trial accounts can only call verified numbers",
            }
        },
    )
    tokens, _, agent_id = await _outbound_ready(client, "trial-acct")
    resp = await _dial(client, tokens, agent_id)
    assert resp.status_code == 424
    assert "verified numbers" in resp.json()["detail"]


async def test_the_answered_call_streams_into_the_same_call_not_a_new_one(
    client: AsyncClient, monkeypatch
):
    _exotel_dials(monkeypatch)
    tokens, key, agent_id = await _outbound_ready(client, "answered")
    call_id = (await _dial(client, tokens, agent_id)).json()["id"]

    # Exotel reports the customer as "from" and the ExoPhone as "to" on an outbound call.
    resp = await client.post(
        "/api/v1/internal/exotel/start",
        headers=_internal(),
        json=_start(key, call_sid="ex-out-1", **{"from": "09800000001", "to": "08000000002"}),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["call_id"] == call_id
    async with session_scope() as db:
        calls = (await db.execute(select(Call))).scalars().all()
        assert len(calls) == 1
        assert calls[0].status.value == "in_progress" and calls[0].started_at is not None


async def test_a_stream_with_an_unexpected_sid_still_finds_the_ringing_call(
    client: AsyncClient, monkeypatch
):
    _exotel_dials(monkeypatch)
    tokens, key, agent_id = await _outbound_ready(client, "other-sid")
    call_id = (await _dial(client, tokens, agent_id)).json()["id"]
    resp = await client.post(
        "/api/v1/internal/exotel/start",
        headers=_internal(),
        json=_start(key, call_sid="leg-2", **{"from": "09800000001", "to": "08000000002"}),
    )
    assert resp.json()["call_id"] == call_id


async def test_an_unanswered_call_is_closed_by_exotels_callback(client: AsyncClient, monkeypatch):
    sent = _exotel_dials(monkeypatch)
    tokens, _, agent_id = await _outbound_ready(client, "no-answer")
    call_id = (await _dial(client, tokens, agent_id)).json()["id"]
    callback = sent[0]["data"]["StatusCallback"]
    path = callback[callback.index("/api/v1/") :]

    forged = path.split("&t=")[0] + "&t=0000"
    assert (await client.post(forged, json={"Status": "no-answer"})).status_code == 403

    resp = await client.post(path, json={"CallSid": "ex-out-1", "Status": "no-answer"})
    assert resp.status_code == 200
    async with session_scope() as db:
        call = await db.get(Call, uuid.UUID(call_id))
        assert call.status.value == "no_answer" and call.ended_at is not None


async def test_the_flow_app_id_must_be_a_number_and_survives_reconnecting(client: AsyncClient):
    tokens, _, _ = await _outbound_ready(client, "keep-flow")
    bad = await client.put(
        "/api/v1/integrations/exotel/outbound", headers=_auth(tokens), json={"app_id": "my flow"}
    )
    assert bad.status_code == 422

    again = await client.post("/api/v1/integrations/exotel", headers=_auth(tokens), json=CREDS)
    assert again.json()["integration"]["config"]["outbound_app_id"] == "1367464"
