"""Voice calls from a web page: the public start endpoint, plan guards, and the signed gateway address."""

import time

from app.core.config import get_settings
from app.services import web_call
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import Call, CallDirection, CallStatus

from .test_channels import _agent, _enable, _signup


async def _voice_channel(client: AsyncClient, slug: str, *, voice: bool = True) -> str:
    auth = await _signup(client, slug=slug)
    agent_id = await _agent(client, auth)
    config = {"title": "Talk to us"}
    if voice:
        config["voice_enabled"] = True
    row = await _enable(client, auth, "web_chat", ai_agent_id=agent_id, config=config)
    return row["public_key"]


def test_signature_round_trip_and_tampering() -> None:
    exp, sig = web_call.sign("call-1", now=1000)
    assert web_call.verify("call-1", exp, sig, now=1001)
    assert not web_call.verify("call-2", exp, sig, now=1001)  # another call's address
    assert not web_call.verify("call-1", exp + 1, sig, now=1001)  # a stretched expiry
    assert not web_call.verify("call-1", exp, sig[:-1] + ("0" if sig[-1] != "0" else "1"), now=1001)
    assert not web_call.verify("call-1", exp, sig, now=exp + 1)  # expired


def test_gateway_url_points_at_the_web_call_endpoint() -> None:
    url = web_call.gateway_url("abc")
    assert "/ws/web-call?call=abc&exp=" in url and "&sig=" in url
    assert "/ws/twilio" not in url
    exp = int(url.split("exp=")[1].split("&")[0])
    assert exp > time.time()


async def test_start_creates_a_real_call_and_a_signed_address(client: AsyncClient, db_session: AsyncSession) -> None:
    key = await _voice_channel(client, "web-call-co")
    resp = await client.post(f"/api/v1/channels/webcall/{key}/start", json={})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["expires_in"] == web_call.TTL_SECONDS
    call = (await db_session.execute(select(Call).where(Call.id == body["call_id"]))).scalar_one()
    assert call.direction == CallDirection.INBOUND and call.status == CallStatus.RINGING
    assert call.from_number == "web" and call.ai_agent_id is not None
    sig = body["ws_url"].split("sig=")[1]
    exp = int(body["ws_url"].split("exp=")[1].split("&")[0])
    assert web_call.verify(body["call_id"], exp, sig)


async def test_start_is_refused_when_voice_is_not_turned_on(client: AsyncClient) -> None:
    key = await _voice_channel(client, "web-call-off", voice=False)
    assert (await client.post(f"/api/v1/channels/webcall/{key}/start", json={})).status_code == 404
    assert (await client.get(f"/api/v1/channels/webcall/{key}/config")).status_code == 404
    assert (await client.post("/api/v1/channels/webcall/no-such-key/start", json={})).status_code == 404


async def test_config_reports_voice_when_enabled(client: AsyncClient) -> None:
    key = await _voice_channel(client, "web-call-cfg")
    resp = await client.get(f"/api/v1/channels/webcall/{key}/config")
    assert resp.status_code == 200 and resp.json() == {"enabled": True, "title": "Talk to us"}


async def test_one_visitor_cannot_start_calls_without_limit(client: AsyncClient) -> None:
    key = await _voice_channel(client, "web-call-limit")
    codes = [(await client.post(f"/api/v1/channels/webcall/{key}/start", json={})).status_code for _ in range(6)]
    assert codes[:4] == [201] * 4 and 429 in codes[4:]


async def test_origin_not_on_the_allow_list_is_refused(client: AsyncClient) -> None:
    auth = await _signup(client, slug="web-call-origin")
    agent_id = await _agent(client, auth)
    row = await _enable(
        client, auth, "web_chat", ai_agent_id=agent_id, config={"voice_enabled": True, "allowed_origins": ["https://shop.test"]}
    )
    url = f"/api/v1/channels/webcall/{row['public_key']}/start"
    assert (await client.post(url, json={}, headers={"Origin": "https://evil.test"})).status_code == 403
    assert (await client.post(url, json={}, headers={"Origin": "https://shop.test"})).status_code == 201


async def test_demo_endpoint_follows_the_setting(client: AsyncClient, monkeypatch) -> None:
    get_settings.cache_clear()
    try:
        assert (await client.get("/api/v1/channels/webcall/demo")).json() == {"enabled": False, "key": None}
        monkeypatch.setenv("WEB_CALL_DEMO_KEY", "demo-key-1")
        get_settings.cache_clear()
        assert (await client.get("/api/v1/channels/webcall/demo")).json() == {"enabled": True, "key": "demo-key-1"}
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
