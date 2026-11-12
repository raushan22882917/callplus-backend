import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.services import channels as channel_svc
from app.services import email_compliance, outreach
from app.services.operations import FOLLOW_UP_RETRY_DELAY, execute_follow_up
from app.tools.context import ToolContext
from app.tools.operations_tools import reach_out_to_customer
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import (
    ConversationChannel,
    Customer,
    DndEntry,
    FollowUp,
    MessageDirection,
    MessageSender,
    Tenant,
)

PHONE, EMAIL = "+919800000001", "priya@example.com"
PLAN = {
    "order": ["whatsapp", "sms", "email"],
    "subject": "Sorry we missed you",
    "messages": {"whatsapp": "Hi Priya, sorry we missed you.", "sms": "Hi Priya, sorry we missed you.", "email": "Hi Priya, sorry."},
}


async def _setup(db: AsyncSession, **payload) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    tenant = Tenant(name="Shop", slug="shop-outreach", timezone="UTC")
    db.add(tenant)
    await db.flush()
    customer = Customer(tenant_id=tenant.id, name="Priya Sharma", phone=PHONE, email=EMAIL)
    db.add(customer)
    await db.flush()
    follow_up = FollowUp(tenant_id=tenant.id, customer_id=customer.id, channel="auto",
                         due_at=datetime.now(UTC) - timedelta(minutes=1), created_by="ai",
                         payload={"goal": "We missed their call. Offer help.", **payload})
    db.add(follow_up)
    await db.commit()
    return tenant.id, customer.id, follow_up.id


def _stub(monkeypatch: pytest.MonkeyPatch, *, usable=("whatsapp", "sms", "email"), fail=()) -> list[str]:
    sent: list[str] = []

    async def usable_channels(db, tenant_id, customer, payload):
        return list(usable), {}

    async def plan(db, tenant_id, customer, goal, usable_now, business, language):
        return {**PLAN, "messages": {c: t for c, t in PLAN["messages"].items() if c in usable_now}}

    async def send(db, tenant_id, customer, channel, plan_, payload):
        if channel in fail:
            raise outreach.sender.FollowUpError(f"{channel} is down")
        sent.append(channel)

    monkeypatch.setattr(outreach, "usable_channels", usable_channels)
    monkeypatch.setattr(outreach, "plan_outreach", plan)
    monkeypatch.setattr(outreach, "_send", send)
    monkeypatch.setattr(outreach, "local_hours_open", lambda now, tz: None)
    return sent


async def _run(db: AsyncSession, follow_up_id: uuid.UUID, now: datetime | None = None) -> FollowUp:
    follow_up = await db.get(FollowUp, follow_up_id)
    await execute_follow_up(db, follow_up, now=now)
    await db.commit()
    await db.refresh(follow_up)
    return follow_up


# ---- the pieces ---------------------------------------------------------------------------------


def test_the_ai_answer_is_cleaned_to_usable_channels_with_a_message() -> None:
    raw = ('<think>hmm</think>{"order": ["email", "telegram", "sms"], "subject": "Hi", "sms": "Hello", '
           '"email": "Body", "whatsapp": "ignored"}')
    plan = outreach.parse_plan(raw, ["sms", "email"])
    assert plan == {"order": ["email", "sms"], "subject": "Hi", "messages": {"sms": "Hello", "email": "Body"}}
    # a channel it forgot to order still gets used, after the ones it chose
    assert outreach.parse_plan('{"order": ["sms"], "sms": "a", "email": "b"}', ["sms", "email"])["order"] == ["sms", "email"]
    assert outreach.parse_plan("not json", ["sms"]) is None
    assert outreach.parse_plan('{"order": ["sms"]}', ["sms"]) is None  # no message written
    assert len(outreach.parse_plan('{"sms": "' + "x" * 900 + '"}', ["sms"])["messages"]["sms"]) == 320


def test_automatic_messages_wait_for_business_hours() -> None:
    noon = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
    assert outreach.local_hours_open(noon, "UTC") is None
    assert outreach.local_hours_open(datetime(2026, 11, 12, 22, 0, tzinfo=UTC), "UTC") == datetime(2026, 11, 13, 9, 0, tzinfo=UTC)
    assert outreach.local_hours_open(datetime(2026, 11, 12, 3, 0, tzinfo=UTC), "UTC") == datetime(2026, 11, 12, 9, 0, tzinfo=UTC)
    # 12:00 UTC is 17:30 in Kolkata (open); 17:00 UTC is 22:30 there (closed until 09:00 IST = 03:30 UTC)
    assert outreach.local_hours_open(noon, "Asia/Kolkata") is None
    assert outreach.local_hours_open(datetime(2026, 11, 12, 17, 0, tzinfo=UTC), "Asia/Kolkata") == datetime(2026, 11, 13, 3, 30, tzinfo=UTC)
    assert outreach.local_hours_open(noon, "Not/AZone") is None  # unknown zone falls back to UTC


# ---- the follow-up, step by step ----------------------------------------------------------------


async def test_it_tries_the_next_channel_when_nobody_answers(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _stub(monkeypatch)
    _, _, follow_up_id = await _setup(db_session)
    now = datetime.now(UTC)

    step = await _run(db_session, follow_up_id, now)
    assert sent == ["whatsapp"] and step.status == "pending"
    assert step.due_at == now + timedelta(minutes=outreach.DEFAULT_WAIT_MINUTES)

    step = await _run(db_session, follow_up_id, now + timedelta(hours=3))
    assert sent == ["whatsapp", "sms"] and step.status == "pending"

    step = await _run(db_session, follow_up_id, now + timedelta(hours=6))
    assert sent == ["whatsapp", "sms", "email"] and step.status == "done"
    assert [t["channel"] for t in step.payload["auto"]["tried"]] == ["whatsapp", "sms", "email"]


async def test_a_failing_channel_falls_back_straight_away(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _stub(monkeypatch, fail=("whatsapp",))
    _, _, follow_up_id = await _setup(db_session)
    step = await _run(db_session, follow_up_id)
    assert sent == ["sms"] and step.status == "pending"
    tried = step.payload["auto"]["tried"]
    assert tried[0]["ok"] is False and "whatsapp is down" in tried[0]["error"] and tried[1] == {**tried[1], "channel": "sms", "ok": True}


async def test_it_stops_as_soon_as_the_customer_answers(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _stub(monkeypatch)
    tenant_id, customer_id, follow_up_id = await _setup(db_session)
    now = datetime.now(UTC)
    await _run(db_session, follow_up_id, now)
    conversation, _, _ = await channel_svc.ensure_thread(
        db_session, tenant_id, ConversationChannel.WHATSAPP, PHONE.lstrip("+"), customer_id=customer_id
    )
    await channel_svc.record_message(
        db_session, tenant_id, conversation.id, direction=MessageDirection.INBOUND,
        sender=MessageSender.CUSTOMER, text="Yes please call me",
    )
    await db_session.commit()

    step = await _run(db_session, follow_up_id, now + timedelta(hours=3))
    assert sent == ["whatsapp"]  # no second message
    assert step.status == "done" and step.payload["auto"]["result"] == "customer answered"


async def test_when_the_ai_is_busy_the_step_is_retried_not_sent_blind(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _stub(monkeypatch)

    async def busy(*_a, **_k):
        raise outreach.OutreachError("the AI helper was busy")

    monkeypatch.setattr(outreach, "plan_outreach", busy)
    _, _, follow_up_id = await _setup(db_session)
    now = datetime.now(UTC)
    step = await _run(db_session, follow_up_id, now)
    assert sent == [] and step.status == "pending" and step.due_at == now + FOLLOW_UP_RETRY_DELAY
    assert "AI outreach" in step.last_error


async def test_outside_business_hours_it_waits_without_using_an_attempt(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _stub(monkeypatch)
    opens = datetime.now(UTC) + timedelta(hours=8)
    monkeypatch.setattr(outreach, "local_hours_open", lambda now, tz: opens)
    _, _, follow_up_id = await _setup(db_session)
    step = await _run(db_session, follow_up_id)
    assert sent == [] and step.status == "pending" and step.due_at == opens and step.attempts == 0


async def test_opted_out_people_are_never_contacted_unless_it_is_a_service_message(db_session: AsyncSession) -> None:
    tenant_id, customer_id, _ = await _setup(db_session)
    db_session.add(DndEntry(tenant_id=tenant_id, e164_number=PHONE, source="opt_out_linked"))
    await email_compliance.suppress(db_session, tenant_id, EMAIL, reason="unsubscribed", source="one_click")
    await db_session.commit()
    customer = await db_session.get(Customer, customer_id)

    usable, skipped = await outreach.usable_channels(db_session, tenant_id, customer, {})
    assert usable == []
    assert skipped["whatsapp"] == skipped["sms"] == "number opted out"
    assert skipped["email"] == "recipient unsubscribed"

    _, skipped = await outreach.usable_channels(db_session, tenant_id, customer, {"purpose": "service"})
    assert skipped["whatsapp"] == "WhatsApp is not connected"  # the opt-out no longer blocks a service message


async def test_with_no_usable_channel_it_fails_and_says_why(db_session: AsyncSession) -> None:
    tenant_id, _, follow_up_id = await _setup(db_session)
    db_session.add(DndEntry(tenant_id=tenant_id, e164_number=PHONE, source="opt_out_linked"))
    await email_compliance.suppress(db_session, tenant_id, EMAIL, reason="unsubscribed", source="one_click")
    await db_session.commit()
    step = await _run(db_session, follow_up_id, datetime(2026, 11, 12, 12, 0, tzinfo=UTC))
    assert step.status == "failed" and "number opted out" in step.last_error and "recipient unsubscribed" in step.last_error


# ---- creating one -------------------------------------------------------------------------------


async def _owner(client: AsyncClient, slug: str) -> dict:
    resp = await client.post("/api/v1/auth/signup", json={"company_name": slug.title(), "tenant_slug": slug,
                             "full_name": "Owner", "email": f"owner@{slug}.com", "password": "supersecret1"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def test_the_api_takes_an_auto_follow_up_with_a_goal(client: AsyncClient, db_session: AsyncSession) -> None:
    headers = await _owner(client, "auto-co")
    customer = (await client.post("/api/v1/customers", headers=headers, json={"name": "Priya", "phone": PHONE, "email": EMAIL})).json()
    due = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    body = {"customer_id": customer["id"], "channel": "auto", "due_at": due}
    assert (await client.post("/api/v1/operations/follow-ups", headers=headers, json=body)).status_code == 400  # no goal
    made = await client.post("/api/v1/operations/follow-ups", headers=headers, json={**body, "goal": "Remind about Friday", "fallback_after_minutes": 30})
    assert made.status_code == 201, made.text
    assert made.json()["channel"] == "auto"
    assert made.json()["payload"] == {"goal": "Remind about Friday", "fallback_after_minutes": 30, "purpose": "marketing"}


async def test_the_ai_agent_tool_schedules_an_auto_follow_up(db_session: AsyncSession) -> None:
    tenant_id, customer_id, _ = await _setup(db_session)
    ctx = ToolContext(db=db_session, tenant_id=tenant_id, agent_id=None, call_id=None, conversation_id=None, customer_id=customer_id)
    assert (await reach_out_to_customer(ctx, goal=""))["error"]
    result = await reach_out_to_customer(ctx, goal="Offer them a slot tomorrow", delay_minutes=0)
    assert result["scheduled"] is True
    row = await db_session.get(FollowUp, uuid.UUID(result["follow_up_id"]))
    assert row.channel == "auto" and row.created_by == "ai"
    assert row.payload == {"goal": "Offer them a slot tomorrow", "purpose": "marketing"}
