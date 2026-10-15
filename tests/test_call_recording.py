import pytest
from app.api.v1 import calls as calls_module
from app.api.v1 import internal as internal_module
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.test_call_control import (
    _auth,
    _internal_headers,
    _make_in_progress_call,
    _signup,
)


async def _setup_call(client, db_session, slug: str, *, recording: bool):
    tokens = await _signup(client, slug=slug, email=f"owner@{slug}.com")
    if recording:
        resp = await client.patch(
            "/api/v1/tenants/me", headers=_auth(tokens), json={"call_recording_enabled": True}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["call_recording_enabled"] is True
    agent = await client.post("/api/v1/ai-agents", headers=_auth(tokens), json={"name": "Bot"})
    call = await _make_in_progress_call(
        db_session, tenant_slug=slug, agent_id=agent.json()["id"], started_seconds_ago=5
    )
    call.provider_call_sid = f"CA-{slug}"
    await db_session.commit()
    return tokens, call


@pytest.fixture
def recording_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    started: list[dict] = []

    async def fake_credentials(db, tenant_id):
        return None

    monkeypatch.setattr(internal_module, "get_twilio_credentials", fake_credentials)
    monkeypatch.setattr(
        internal_module, "start_call_recording", lambda **kwargs: started.append(kwargs)
    )
    return started


async def test_recording_is_off_by_default(
    client: AsyncClient, db_session: AsyncSession, recording_calls: list[dict]
) -> None:
    _, call = await _setup_call(client, db_session, "rec-off", recording=False)
    resp = await client.get(
        f"/api/v1/internal/calls/{call.id}/config", headers=_internal_headers()
    )
    assert resp.status_code == 200
    assert recording_calls == []


async def test_recording_starts_when_tenant_opted_in(
    client: AsyncClient, db_session: AsyncSession, recording_calls: list[dict]
) -> None:
    _, call = await _setup_call(client, db_session, "rec-on", recording=True)
    resp = await client.get(
        f"/api/v1/internal/calls/{call.id}/config", headers=_internal_headers()
    )
    assert resp.status_code == 200
    assert len(recording_calls) == 1
    assert recording_calls[0]["call_sid"] == "CA-rec-on"
    assert recording_calls[0]["status_callback_url"].endswith("/twilio/recording")


async def test_recording_webhook_stores_mp3_url_and_playback_is_audited(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tokens, call = await _setup_call(client, db_session, "rec-play", recording=True)

    no_recording = await client.get(f"/api/v1/calls/{call.id}/recording", headers=_auth(tokens))
    assert no_recording.status_code == 404

    url = "https://api.twilio.com/2010-04-01/Accounts/AC1/Recordings/RE1"
    hook = await client.post(
        "/api/v1/webhooks/telephony/twilio/recording",
        data={"CallSid": "CA-rec-play", "RecordingUrl": url, "RecordingStatus": "completed"},
    )
    assert hook.status_code == 200

    class FakeResponse:
        status_code = 200
        content = b"ID3-fake-mp3"

    class FakeClient:
        def __init__(self, **kwargs): ...
        async def __aenter__(self):
            return self
        async def __aexit__(self, *exc): ...
        async def get(self, requested_url, auth):
            assert requested_url == f"{url}.mp3"
            return FakeResponse()

    monkeypatch.setattr(calls_module.httpx, "AsyncClient", FakeClient)
    resp = await client.get(f"/api/v1/calls/{call.id}/recording", headers=_auth(tokens))
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "audio/mpeg"
    assert resp.content == b"ID3-fake-mp3"

    audit = await client.get("/api/v1/audit-logs", headers=_auth(tokens))
    assert any(e["action"] == "call.recording_accessed" for e in audit.json())


async def test_recording_playback_refuses_non_twilio_hosts(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    tokens, call = await _setup_call(client, db_session, "rec-ssrf", recording=True)
    call.recording_url = "http://169.254.169.254/latest/meta-data"
    await db_session.commit()
    resp = await client.get(f"/api/v1/calls/{call.id}/recording", headers=_auth(tokens))
    assert resp.status_code == 502
