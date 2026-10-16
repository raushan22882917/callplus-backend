"""Outbound answering-machine detection: Twilio params, verdict handling, webhook."""

import pytest
from app.integrations import twilio_client
from app.services import voicemail_detection as amd
from httpx import AsyncClient
from sqlalchemy import select

from packages.database.models import AIAgent, Call, CallDirection, CallStatus, Tenant
from packages.database.session import session_scope


async def _setup(detect=True, message=None, status=CallStatus.IN_PROGRESS):
    async with session_scope() as db:
        tenant = Tenant(name="Amd Co", slug="amd-co")
        db.add(tenant)
        await db.flush()
        agent = AIAgent(
            tenant_id=tenant.id, name="a", detect_voicemail=detect, voicemail_message=message
        )
        db.add(agent)
        await db.flush()
        call = Call(
            tenant_id=tenant.id,
            ai_agent_id=agent.id,
            direction=CallDirection.OUTBOUND,
            status=status,
            provider_call_sid="CA123",
            to_number="+15551230000",
        )
        db.add(call)
        await db.commit()
        return agent.id, call.id


@pytest.fixture
def redirects(monkeypatch):
    sent: list[str] = []

    async def creds(db, tenant_id):
        return None

    monkeypatch.setattr(amd, "get_twilio_credentials", creds)
    monkeypatch.setattr(amd, "redirect_call", lambda *, call_sid, twiml, credentials=None: sent.append(twiml))
    return sent


async def _verdict(call_id, answered_by):
    async with session_scope() as db:
        call = await db.get(Call, call_id)
        action = await amd.handle_answered_by(db, call, answered_by)
    async with session_scope() as db:
        return action, await db.get(Call, call_id)


async def test_human_is_left_alone(redirects):
    _, call_id = await _setup()
    action, call = await _verdict(call_id, "human")
    assert action == "none" and redirects == [] and call.answered_by == "human" and call.outcome is None


async def test_machine_without_message_hangs_up(redirects):
    _, call_id = await _setup()
    action, call = await _verdict(call_id, "machine_end_beep")
    assert action == "hangup" and "<Hangup" in redirects[0] and "<Say" not in redirects[0]
    assert call.answered_by == "machine_end_beep" and call.outcome == "voicemail"


async def test_machine_with_message_speaks_it_after_the_beep_escaped(redirects):
    _, call_id = await _setup(message="Hi <b>Sam</b> & co, call us back.")
    action, _ = await _verdict(call_id, "machine_end_silence")
    assert action == "voicemail"
    assert "&lt;b&gt;Sam&lt;/b&gt; &amp; co" in redirects[0] and "<Hangup" in redirects[0]


async def test_machine_still_greeting_or_fax_just_hangs_up(redirects):
    _, call_id = await _setup(message="Please call back")
    assert (await _verdict(call_id, "machine_start"))[0] == "hangup"
    assert "<Say" not in redirects[0]
    _, fax_call = await _setup_second()
    assert (await _verdict(fax_call, "fax"))[0] == "hangup"


async def _setup_second():
    async with session_scope() as db:
        tenant = (await db.execute(select(Tenant))).scalars().first()
        call = Call(tenant_id=tenant.id, direction=CallDirection.OUTBOUND, status=CallStatus.IN_PROGRESS, provider_call_sid="CA999")
        db.add(call)
        await db.commit()
        return tenant.id, call.id


async def test_unknown_verdict_is_treated_as_unknown(redirects):
    _, call_id = await _setup()
    action, call = await _verdict(call_id, "something-new")
    assert action == "none" and call.answered_by == "unknown"


async def test_callback_only_for_agents_that_opt_in():
    on, _ = await _setup(detect=True)
    async with session_scope() as db:
        assert (await amd.amd_callback_for_agent(db, on)).endswith("/twilio/amd")
        assert await amd.amd_callback_for_agent(db, None) is None
        agent = await db.get(AIAgent, on)
        agent.detect_voicemail = False
        await db.commit()
        assert await amd.amd_callback_for_agent(db, on) is None


def test_twilio_call_gets_async_amd_only_when_requested(monkeypatch):
    created: list[dict] = []

    class _Calls:
        def create(self, **kwargs):
            created.append(kwargs)
            return type("C", (), {"sid": "CAx"})()

    monkeypatch.setattr(twilio_client, "_client", lambda credentials=None: type("T", (), {"calls": _Calls()})())
    twilio_client.place_outbound_call(to="+1", from_="+2", voice_webhook_url="https://x/v")
    assert "machine_detection" not in created[0]
    twilio_client.place_outbound_call(
        to="+1", from_="+2", voice_webhook_url="https://x/v", amd_callback_url="https://x/amd"
    )
    assert created[1]["machine_detection"] == "DetectMessageEnd"
    assert created[1]["async_amd"] == "true" and created[1]["async_amd_status_callback"] == "https://x/amd"


async def test_webhook_records_verdict_and_ignores_finished_calls(client: AsyncClient, redirects):
    _, call_id = await _setup()
    resp = await client.post(
        "/api/v1/webhooks/telephony/twilio/amd", data={"CallSid": "CA123", "AnsweredBy": "machine_end_beep"}
    )
    assert resp.status_code == 200 and len(redirects) == 1
    async with session_scope() as db:
        call = await db.get(Call, call_id)
        assert call.answered_by == "machine_end_beep"
        call.status = CallStatus.COMPLETED
        call.answered_by = None
        await db.commit()
    await client.post("/api/v1/webhooks/telephony/twilio/amd", data={"CallSid": "CA123", "AnsweredBy": "machine_end_beep"})
    assert len(redirects) == 1  # finished call untouched
    unknown = await client.post("/api/v1/webhooks/telephony/twilio/amd", data={"CallSid": "nope", "AnsweredBy": "human"})
    assert unknown.status_code == 200


async def test_agent_api_exposes_the_settings(client: AsyncClient):
    signup = await client.post(
        "/api/v1/auth/signup",
        json={"company_name": "Agent Co", "tenant_slug": "agent-co", "full_name": "O", "email": "o@agent-co.com", "password": "supersecret1"},
    )
    headers = {"Authorization": f"Bearer {signup.json()['access_token']}"}
    created = await client.post(
        "/api/v1/ai-agents", headers=headers, json={"name": "Dialer", "detect_voicemail": True, "voicemail_message": "Call us back"}
    )
    assert created.status_code == 201, created.text
    assert created.json()["detect_voicemail"] is True and created.json()["voicemail_message"] == "Call us back"
    patched = await client.patch(f"/api/v1/ai-agents/{created.json()['id']}", headers=headers, json={"detect_voicemail": False})
    assert patched.status_code == 200 and patched.json()["detect_voicemail"] is False
    too_long = await client.post("/api/v1/ai-agents", headers=headers, json={"name": "X", "voicemail_message": "a" * 501})
    assert too_long.status_code == 422


def test_redirect_live_maps_twilio_not_in_progress_to_call_not_live() -> None:
    from unittest.mock import MagicMock

    from app.integrations.twilio_client import CallNotLiveError, TwilioClientError, redirect_live
    from twilio.base.exceptions import TwilioRestException

    def client_raising(code: int) -> MagicMock:
        client = MagicMock()
        client.calls.return_value.update.side_effect = TwilioRestException(
            400, "/Calls/CA1", "Unable to update record: Call is not in-progress. Cannot redirect.", code
        )
        return client

    with pytest.raises(CallNotLiveError):
        redirect_live(client=client_raising(21220), call_sid="CA1", twiml="<Response/>")
    with pytest.raises(CallNotLiveError):
        redirect_live(client=client_raising(20404), call_sid="CA1", twiml="<Response/>")
    # still a TwilioClientError, so existing handlers keep working
    assert issubclass(CallNotLiveError, TwilioClientError)
    # any other Twilio failure is not swallowed or relabelled
    with pytest.raises(TwilioRestException):
        redirect_live(client=client_raising(21211), call_sid="CA1", twiml="<Response/>")
