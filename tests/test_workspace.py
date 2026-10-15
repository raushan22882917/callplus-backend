import uuid
from datetime import UTC, date, datetime

import pytest
from app.api.v1 import workspace as workspace_api
from app.integrations import twilio_workspace as tw
from app.integrations.credentials import TwilioCredentials
from app.main import app
from app.services.routing import is_open, pick_targets, rank_agents, score_agent
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import (
    Call,
    CallEvent,
    Conversation,
    ConversationChannel,
    Customer,
    Message,
    MessageDirection,
    MessageSender,
    Ticket,
)
from packages.database.models.workspace import (
    AgentProfile,
    BusinessHours,
    CallHandoff,
    HandlerState,
    Holiday,
    HumanAgentStatus,
)
from tests.test_call_control import _auth, _internal_headers, _make_in_progress_call, _signup

if not any(getattr(r, "path", "") == "/api/v1/workspace/live" for r in app.routes):
    app.include_router(workspace_api.router, prefix="/api/v1")


def _profile(**kw) -> AgentProfile:
    defaults = dict(
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        status=HumanAgentStatus.AVAILABLE,
        phone_number="+911111111111",
        skills=[],
        languages=[],
        departments=[],
        is_vip_handler=False,
        max_concurrent=1,
        status_changed_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    defaults.update(kw)
    return AgentProfile(**defaults)


WEEKDAYS = [{"weekday": d, "start": "09:00", "end": "17:00"} for d in range(5)]


def test_is_open_respects_timezone_windows_and_holidays() -> None:
    # 2026-10-05 is a Monday. 04:00 UTC = 09:30 IST (open); 12:00 UTC = 17:30 IST (closed).
    open_ist = datetime(2026, 10, 5, 4, 0, tzinfo=UTC)
    closed_ist = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
    kwargs = dict(timezone="Asia/Kolkata", windows=WEEKDAYS)
    assert is_open(**kwargs, holidays=set(), now=open_ist)[0] is True
    assert is_open(**kwargs, holidays=set(), now=closed_ist) == (False, "outside business hours")
    assert is_open(**kwargs, holidays={date(2026, 10, 5)}, now=open_ist) == (False, "holiday")
    sunday = datetime(2026, 10, 4, 4, 0, tzinfo=UTC)
    assert is_open(**kwargs, holidays=set(), now=sunday)[0] is False


def test_scoring_prefers_language_skill_department_and_vip() -> None:
    plain = _profile()
    hindi = _profile(languages=["hi"], skills=["billing"], departments=["Finance"])
    kwargs = dict(language="hi-IN", department="finance", vip=False, issue_type="Billing")
    assert score_agent(hindi, **kwargs, active_calls=0) == 40 + 30 + 25
    # language miss (-20) + generalist-with-no-departments bonus (+5)
    assert score_agent(plain, **kwargs, active_calls=0) == -15
    vip_agent = _profile(is_vip_handler=True)
    assert (
        score_agent(vip_agent, language=None, department=None, vip=True, issue_type=None, active_calls=0)
        == 50
    )
    # Non-VIP calls keep VIP handlers in reserve; load lowers the score.
    assert (
        score_agent(vip_agent, language=None, department=None, vip=False, issue_type=None, active_calls=0)
        == -5
    )
    assert (
        score_agent(plain, language=None, department=None, vip=False, issue_type=None, active_calls=1)
        == -10
    )


def test_rank_filters_unavailable_and_full_agents_and_orders_by_score() -> None:
    best = _profile(phone_number="+911000000001", languages=["hi"])
    other = _profile(phone_number="+911000000002")
    away = _profile(phone_number="+911000000003", status=HumanAgentStatus.AWAY, languages=["hi"])
    no_phone = _profile(phone_number=None, languages=["hi"])
    full = _profile(phone_number="+911000000004", languages=["hi"])
    ranked = rank_agents(
        [other, away, no_phone, full, best],
        {full.user_id: 1},
        language="hi",
        department=None,
        vip=False,
        issue_type=None,
    )
    assert [t.phone_number for t in ranked] == ["+911000000001", "+911000000002"]


async def _tenant_id(db: AsyncSession, slug: str) -> uuid.UUID:
    from packages.database.models import Tenant

    return (await db.execute(select(Tenant.id).where(Tenant.slug == slug))).scalar_one()


async def _member_ids(db: AsyncSession, tenant_id: uuid.UUID) -> list[uuid.UUID]:
    from packages.database.models import TenantUser

    return list(
        (await db.execute(select(TenantUser.user_id).where(TenantUser.tenant_id == tenant_id)))
        .scalars()
        .all()
    )


async def test_pick_targets_legacy_ring_fallback_and_hours(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _signup(client, slug="route-co", email="owner@routeco.com")
    tenant_id = await _tenant_id(db_session, "route-co")
    (owner_id,) = await _member_ids(db_session, tenant_id)
    monday_open = datetime(2026, 10, 5, 4, 0, tzinfo=UTC)

    # No profiles at all -> caller keeps the legacy ring-all behaviour.
    assert (await pick_targets(db_session, tenant_id, now=monday_open)).action == "legacy"

    profile = _profile(tenant_id=tenant_id, user_id=owner_id, phone_number="+919000000001")
    db_session.add(profile)
    await db_session.commit()
    decision = await pick_targets(db_session, tenant_id, language="en", now=monday_open)
    assert decision.action == "ring"
    assert [t.phone_number for t in decision.targets] == ["+919000000001"]

    # Nobody available -> voicemail fallback by default.
    profile.status = HumanAgentStatus.BUSY
    await db_session.commit()
    assert (await pick_targets(db_session, tenant_id, now=monday_open)).action == "voicemail"

    # Business hours: closed on a holiday even with an agent available.
    profile.status = HumanAgentStatus.AVAILABLE
    db_session.add(
        BusinessHours(
            tenant_id=tenant_id,
            timezone="Asia/Kolkata",
            windows=WEEKDAYS,
            fallback_action="callback",
            fallback_message="We will call you back.",
        )
    )
    db_session.add(Holiday(tenant_id=tenant_id, day=date(2026, 10, 5), name="Holiday"))
    await db_session.commit()
    closed = await pick_targets(db_session, tenant_id, now=monday_open)
    assert (closed.action, closed.reason, closed.message) == (
        "callback",
        "holiday",
        "We will call you back.",
    )
    tuesday_open = datetime(2026, 10, 6, 4, 0, tzinfo=UTC)
    assert (await pick_targets(db_session, tenant_id, now=tuesday_open)).action == "ring"


async def test_finalize_transfer_uses_routing_when_profiles_exist(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api.v1 import internal as internal_module

    tokens = await _signup(client, slug="route-xfer", email="owner@routexfer.com")
    tenant_id = await _tenant_id(db_session, "route-xfer")
    (owner_id,) = await _member_ids(db_session, tenant_id)
    db_session.add(_profile(tenant_id=tenant_id, user_id=owner_id, phone_number="+919000000042"))
    await db_session.commit()
    agent = await client.post("/api/v1/ai-agents", headers=_auth(tokens), json={"name": "Bot"})
    call = await _make_in_progress_call(
        db_session, tenant_slug="route-xfer", agent_id=agent.json()["id"], started_seconds_ago=5
    )
    call.provider_call_sid = "CA999"
    call.summary = "already summarised"
    await db_session.commit()

    sent: dict = {}

    async def fake_credentials(db, tenant_id):
        return None

    monkeypatch.setattr(internal_module, "get_twilio_credentials", fake_credentials)
    monkeypatch.setattr(
        internal_module, "redirect_call", lambda *, call_sid, twiml, credentials: sent.update(t=twiml)
    )
    resp = await client.post(
        f"/api/v1/internal/calls/{call.id}/finalize",
        headers=_internal_headers(),
        json={"action": "transfer", "reason": "wants a human"},
    )
    assert resp.status_code == 200, resp.text
    assert "+919000000042" in sent["t"]
    event = (
        await db_session.execute(select(CallEvent).where(CallEvent.type == "TRANSFER_REQUESTED"))
    ).scalar_one()
    assert event.payload["routing"] == "ring"


async def test_handoff_package_contents(client: AsyncClient, db_session: AsyncSession) -> None:
    tokens = await _signup(client, slug="hand-co", email="owner@handco.com")
    agent = await client.post("/api/v1/ai-agents", headers=_auth(tokens), json={"name": "Bot"})
    call = await _make_in_progress_call(
        db_session, tenant_slug="hand-co", agent_id=agent.json()["id"], started_seconds_ago=30
    )
    customer = await db_session.get(Customer, call.customer_id)
    customer.tags = ["VIP"]
    old = Call(
        tenant_id=call.tenant_id,
        customer_id=customer.id,
        direction=call.direction,
        status=call.status.COMPLETED,
        summary="Asked about invoice",
        sentiment="neutral",
        outcome="information_given",
    )
    ticket = Ticket(tenant_id=call.tenant_id, customer_id=customer.id, subject="Refund pending")
    conversation = Conversation(
        tenant_id=call.tenant_id,
        customer_id=customer.id,
        channel=ConversationChannel.VOICE,
        call_id=call.id,
    )
    db_session.add_all([old, ticket, conversation])
    await db_session.flush()
    db_session.add(
        Message(
            tenant_id=call.tenant_id,
            conversation_id=conversation.id,
            direction=MessageDirection.INBOUND,
            sender=MessageSender.CUSTOMER,
            text="Where is my refund?",
        )
    )
    call.summary = "Caller wants refund status"
    call.sentiment = "negative"
    call.intent = "refund status"
    call.recording_url = "https://example.com/r.mp3"
    await db_session.commit()

    resp = await client.get(f"/api/v1/workspace/calls/{call.id}/handoff", headers=_auth(tokens))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["customer"]["name"] == "Jane" and body["customer"]["is_vip"] is True
    assert body["transcript"] == [
        {"sender": "caller", "text": "Where is my refund?", "at": body["transcript"][0]["at"]}
    ]
    assert body["summary"] == "Caller wants refund status"
    assert body["sentiment"] == "negative" and body["intent"] == "refund status"
    assert [c["summary"] for c in body["previous_calls"]] == ["Asked about invoice"]
    assert [t["subject"] for t in body["tickets"]] == ["Refund pending"]
    assert body["suggested_next_action"]["label"] == "Review open ticket: Refund pending"
    assert body["recording_url"] == "https://example.com/r.mp3"
    assert body["handler"] == "ai"

    other = await _signup(client, slug="hand-other", email="owner@handother.com")
    assert (
        await client.get(f"/api/v1/workspace/calls/{call.id}/handoff", headers=_auth(other))
    ).status_code == 404


async def test_takeover_gate_silences_ai_and_keeps_transcript(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api.v1 import internal as internal_module

    tokens = await _signup(client, slug="gate-co", email="owner@gateco.com")
    agent = await client.post("/api/v1/ai-agents", headers=_auth(tokens), json={"name": "Bot"})
    call = await _make_in_progress_call(
        db_session, tenant_slug="gate-co", agent_id=agent.json()["id"], started_seconds_ago=5
    )

    async def boom(**kwargs):
        raise AssertionError("AI must not run while a human controls the call")

    monkeypatch.setattr(internal_module, "run_turn", boom)
    db_session.add(
        CallHandoff(tenant_id=call.tenant_id, call_id=call.id, state=HandlerState.HUMAN)
    )
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/internal/calls/{call.id}/turn",
        headers=_internal_headers(),
        json={"transcript": "hello are you there"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["reply_text"] == "" and resp.json()["action"] == "none"
    messages = (await db_session.execute(select(Message.text))).scalars().all()
    assert messages == ["hello are you there"]

    # Gateway end-signal must not finalize a human-held call.
    end = await client.post(
        f"/api/v1/internal/calls/{call.id}/end",
        headers=_internal_headers(),
        json={"status": "completed", "duration_seconds": 10},
    )
    assert end.status_code == 200 and end.json()["status"] == "in_progress"


async def test_control_endpoints_error_without_twilio_and_work_with_it(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tokens = await _signup(client, slug="ctl-co", email="owner@ctlco.com")
    headers = _auth(tokens)
    agent = await client.post("/api/v1/ai-agents", headers=headers, json={"name": "Bot"})
    call = await _make_in_progress_call(
        db_session, tenant_slug="ctl-co", agent_id=agent.json()["id"], started_seconds_ago=5
    )
    call.provider_call_sid = "CAcustomer"
    call.to_number = "+18005550100"
    await db_session.commit()
    base = f"/api/v1/workspace/calls/{call.id}"

    # Controls on an AI-handled call need a takeover first.
    assert (await client.post(f"{base}/hold", headers=headers)).status_code == 409
    # Supervisor actions need a human-handled call.
    assert (await client.post(f"{base}/supervisor/listen", headers=headers, json={})).status_code == 409

    # No Twilio credentials -> clear 400, and the call stays with the AI.
    missing = await client.post(f"{base}/takeover", headers=headers)
    assert missing.status_code == 400 and "Twilio" in missing.json()["detail"]
    assert (await client.get(f"{base}/handoff", headers=headers)).json()["handler"] == "ai"

    # Agents must set a phone number first.
    async def fake_creds(db, tenant_id):
        return TwilioCredentials(account_sid="ACtest", auth_token="token")

    monkeypatch.setattr(workspace_api, "get_twilio_credentials", fake_creds)
    no_phone = await client.post(f"{base}/takeover", headers=headers)
    assert no_phone.status_code == 400 and "phone" in no_phone.json()["detail"]
    status_resp = await client.put(
        "/api/v1/workspace/me/status",
        headers=headers,
        json={"status": "available", "phone_number": "+919000000007"},
    )
    assert status_resp.status_code == 200 and status_resp.json()["status"] == "available"

    calls: list[tuple[str, dict]] = []

    def record(name, result=None):
        def fn(**kw):
            calls.append((name, kw))
            return result

        return fn

    monkeypatch.setattr(tw, "dial_leg", record("dial", "CAagent"))
    monkeypatch.setattr(tw, "redirect_to_twiml", record("redirect"))
    monkeypatch.setattr(tw, "update_participant", record("participant"))
    monkeypatch.setattr(tw, "hangup_leg", record("hangup"))

    took = await client.post(f"{base}/takeover", headers=headers)
    assert took.status_code == 200, took.text
    assert took.json()["state"] == "human"
    dial = dict(calls)["dial"]
    assert dial["to"] == "+919000000007" and dial["from_"] == "+18005550100"
    assert "<Conference" in dict(calls)["redirect"]["twiml"]

    assert (await client.post(f"{base}/hold", headers=headers)).json()["on_hold"] is True
    assert dict(calls)["participant"]["call_sid"] == "CAcustomer"
    muted = await client.post(f"{base}/mute", headers=headers)
    assert muted.json()["muted"] is True

    warm = await client.post(
        f"{base}/transfer", headers=headers, json={"mode": "warm", "phone_number": "+919000000009"}
    )
    assert warm.status_code == 200, warm.text
    bad = await client.post(f"{base}/transfer", headers=headers, json={"mode": "cold"})
    assert bad.status_code == 422

    sup = await client.post(f"{base}/supervisor/whisper", headers=headers, json={})
    assert sup.status_code == 200, sup.text
    whisper = [kw for name, kw in calls if name == "dial"][-1]
    assert 'coach="CAagent"' in whisper["twiml"]

    live = (await client.get("/api/v1/workspace/live", headers=headers)).json()
    assert live["human_count"] == 1 and live["ai_count"] == 0
    assert live["calls"][0]["handler"] == "human"

    back = await client.post(f"{base}/return-to-ai", headers=headers)
    assert back.status_code == 200 and back.json()["state"] == "ai"
    handoff = (await client.get(f"{base}/handoff", headers=headers)).json()
    assert handoff["handler"] == "ai"

    callback = await client.post(f"{base}/callback", headers=headers, json={"note": "after 5pm"})
    assert callback.status_code == 200 and "ticket created" in callback.json()["detail"]
    assert (await db_session.execute(select(Ticket))).scalars().first().subject.startswith("Callback")


async def test_routing_config_endpoints(client: AsyncClient) -> None:
    tokens = await _signup(client, slug="cfg-co", email="owner@cfgco.com")
    headers = _auth(tokens)
    hours = {
        "timezone": "Asia/Kolkata",
        "windows": [{"weekday": 0, "start": "09:00", "end": "18:00"}],
        "fallback_action": "callback",
        "fallback_message": None,
    }
    assert (await client.put("/api/v1/workspace/business-hours", headers=headers, json=hours)).status_code == 200
    assert (await client.get("/api/v1/workspace/business-hours", headers=headers)).json() == hours
    bad = dict(hours, windows=[{"weekday": 0, "start": "18:00", "end": "09:00"}])
    assert (await client.put("/api/v1/workspace/business-hours", headers=headers, json=bad)).status_code == 422

    created = await client.post(
        "/api/v1/workspace/holidays", headers=headers, json={"day": "2026-12-25", "name": "Xmas"}
    )
    assert created.status_code == 201
    dup = await client.post("/api/v1/workspace/holidays", headers=headers, json={"day": "2026-12-25"})
    assert dup.status_code == 409
    holiday_id = created.json()["id"]
    assert len((await client.get("/api/v1/workspace/holidays", headers=headers)).json()) == 1
    assert (await client.delete(f"/api/v1/workspace/holidays/{holiday_id}", headers=headers)).status_code == 204

    agents = (await client.get("/api/v1/workspace/agents", headers=headers)).json()
    assert len(agents) == 1
    updated = await client.put(
        f"/api/v1/workspace/agents/{agents[0]['user_id']}",
        headers=headers,
        json={"skills": ["billing", "Billing"], "languages": ["hi", "en"], "max_concurrent": 2},
    )
    assert updated.status_code == 200
    assert updated.json()["skills"] == ["billing"] and updated.json()["max_concurrent"] == 2
    unknown = await client.put(
        f"/api/v1/workspace/agents/{uuid.uuid4()}", headers=headers, json={"skills": []}
    )
    assert unknown.status_code == 404
