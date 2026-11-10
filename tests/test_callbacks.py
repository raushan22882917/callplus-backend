"""The AI calls back when asked: "in 5 minutes", "tomorrow at 4 PM", with retries and a record."""

import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.services import callbacks, outbound_call
from app.services.operations import run_due_follow_ups
from app.tools.context import ToolContext
from app.tools.handlers import schedule_callback
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import (
    AIAgent,
    Call,
    CallDirection,
    CallStatus,
    Customer,
    FollowUp,
    MissedCall,
    PhoneNumber,
    Tenant,
    Ticket,
)

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 11, 4, 14, 0, tzinfo=IST)  # 2 PM in India


def test_relative_times():
    due = callbacks.parse_when("call me in 5 minutes", now=NOW, tz=IST)
    assert due == (NOW + timedelta(minutes=5)).astimezone(UTC)
    assert callbacks.parse_when("in an hour", now=NOW, tz=IST) == (
        NOW + timedelta(hours=1)
    ).astimezone(UTC)
    assert callbacks.parse_when("in half an hour", now=NOW, tz=IST) == (
        NOW + timedelta(minutes=30)
    ).astimezone(UTC)
    assert callbacks.parse_when(None, minutes_from_now=10, now=NOW, tz=IST) == (
        NOW + timedelta(minutes=10)
    ).astimezone(UTC)


def test_clock_times_use_the_business_time_zone():
    assert callbacks.parse_when("tomorrow at 4 pm", now=NOW, tz=IST).astimezone(IST) == datetime(
        2026, 11, 5, 16, 0, tzinfo=IST
    )
    assert callbacks.parse_when("2026-11-04 17:30", now=NOW, tz=IST).astimezone(IST) == datetime(
        2026, 11, 4, 17, 30, tzinfo=IST
    )
    # "at 10 am" said at 2 PM can only mean tomorrow
    assert callbacks.parse_when("at 10 am", now=NOW, tz=IST).astimezone(IST).day == 5


def test_nonsense_and_far_future_are_refused_and_the_past_means_now():
    assert callbacks.parse_when("whenever you like", now=NOW, tz=IST) is None
    assert callbacks.parse_when("in 90 days", now=NOW, tz=IST) is None
    soon = callbacks.parse_when(None, minutes_from_now=-5, now=NOW, tz=IST)
    assert soon is not None and soon - NOW.astimezone(UTC) <= timedelta(seconds=30)


def test_a_callback_ticket_keeps_the_day_the_customer_meant():
    created = datetime(2026, 10, 20, 13, 19, tzinfo=IST)
    parsed = callbacks.when_from_callback_subject(
        "Callback requested for today at 4 PM — Air fitting appointment requested by customer",
        created,
        IST,
    )
    assert parsed is not None
    due, note = parsed
    assert due.astimezone(IST) == datetime(2026, 10, 20, 16, 0, tzinfo=IST)
    assert note.startswith("Air fitting")
    tomorrow = callbacks.when_from_callback_subject(
        "Callback requested for tomorrow at 2 PM — Haircut appointment request",
        datetime(2026, 10, 3, 12, 2, tzinfo=IST),
        IST,
    )
    assert tomorrow is not None and tomorrow[0].astimezone(IST) == datetime(2026, 10, 4, 14, 0, tzinfo=IST)


def test_the_ai_can_say_the_time_back():
    due = (NOW + timedelta(minutes=5)).astimezone(UTC)
    assert callbacks.describe(due, IST, NOW) == "in about 5 minutes"
    assert (
        callbacks.describe((NOW + timedelta(days=1, hours=2)).astimezone(UTC), IST, NOW)
        == "tomorrow at 4:00 PM"
    )


async def _setup(db: AsyncSession) -> tuple[Tenant, Customer, Call]:
    tenant = Tenant(name="Salon", slug=f"salon-{uuid.uuid4().hex[:8]}", timezone="Asia/Kolkata")
    db.add(tenant)
    await db.flush()
    agent = AIAgent(tenant_id=tenant.id, name="Reception", greeting="Hi")
    db.add(agent)
    await db.flush()
    db.add(
        PhoneNumber(
            tenant_id=tenant.id,
            e164_number="+919800011111",
            ai_agent_id=agent.id,
            provider="twilio",
        )
    )
    customer = Customer(tenant_id=tenant.id, name="Asha", phone="+919800000001")
    db.add(customer)
    await db.flush()
    call = Call(
        tenant_id=tenant.id,
        customer_id=customer.id,
        ai_agent_id=agent.id,
        direction=CallDirection.INBOUND,
        status=CallStatus.IN_PROGRESS,
        from_number=customer.phone,
        to_number="+919800011111",
    )
    db.add(call)
    await db.commit()
    return tenant, customer, call


def _ctx(db, tenant, customer, call) -> ToolContext:
    return ToolContext(
        db=db,
        tenant_id=tenant.id,
        agent_id=call.ai_agent_id,
        call_id=call.id,
        conversation_id=None,
        customer_id=customer.id,
    )


async def test_asking_for_a_callback_schedules_an_ai_call_and_a_second_ask_moves_it(
    db_session: AsyncSession,
):
    tenant, customer, call = await _setup(db_session)
    ctx = _ctx(db_session, tenant, customer, call)
    result = await schedule_callback(ctx, minutes_from_now=5, note="wants a haircut slot")
    assert result["scheduled"] is True and "5 minutes" in result["call_back"]
    await schedule_callback(ctx, minutes_from_now=20, note="wants a haircut slot")
    await db_session.commit()
    rows = (
        (await db_session.execute(select(FollowUp).where(FollowUp.tenant_id == tenant.id)))
        .scalars()
        .all()
    )
    assert len(rows) == 1  # moved, not duplicated
    assert rows[0].channel == "call" and rows[0].payload["from_number"] == "+919800011111"
    assert rows[0].payload["reason"] == "wants a haircut slot"


async def test_callback_tickets_show_on_the_calendar_at_the_time_that_was_asked(
    db_session: AsyncSession,
):
    tenant, customer, _call = await _setup(db_session)
    ticket = Ticket(
        tenant_id=tenant.id,
        customer_id=customer.id,
        subject="Callback requested for today at 4 PM — Air fitting appointment",
        created_at=datetime(2026, 10, 20, 13, 19, tzinfo=IST),
    )
    db_session.add(ticket)
    await db_session.commit()

    week = await callbacks.calendar_callbacks(
        db_session,
        tenant.id,
        datetime(2026, 10, 20, 0, 0, tzinfo=IST),
        datetime(2026, 10, 21, 0, 0, tzinfo=IST),
    )
    assert len(week["events"]) == 1
    assert week["events"][0]["title"] == "Asha"
    assert week["events"][0]["detail"] == "Air fitting appointment"
    assert week["events"][0]["start_at"].astimezone(IST) == datetime(2026, 10, 20, 16, 0, tzinfo=IST)
    assert week["nearest_at"] is None

    later = await callbacks.calendar_callbacks(
        db_session,
        tenant.id,
        datetime(2026, 11, 9, 0, 0, tzinfo=IST),
        datetime(2026, 11, 16, 0, 0, tzinfo=IST),
    )
    assert later["events"] == []
    assert later["nearest_at"].astimezone(IST) == datetime(2026, 10, 20, 16, 0, tzinfo=IST)


async def test_unclear_time_still_reaches_staff(db_session: AsyncSession):
    tenant, customer, call = await _setup(db_session)
    result = await schedule_callback(
        _ctx(db_session, tenant, customer, call), requested_time="sometime soon"
    )
    assert result.get("scheduled") is None and result["recorded"] is True and result["ticket_id"]


async def test_the_callback_is_placed_when_due_with_a_reason_and_retried_if_unanswered(
    db_session: AsyncSession, monkeypatch
):
    tenant, customer, call = await _setup(db_session)
    await schedule_callback(
        _ctx(db_session, tenant, customer, call), minutes_from_now=5, note="wants a haircut slot"
    )
    await db_session.commit()

    placed: list[dict] = []
    monkeypatch.setattr(
        outbound_call, "place_outbound_call", lambda **kw: placed.append(kw) or "CA1"
    )
    monkeypatch.setattr(outbound_call, "get_twilio_credentials", _async(None))

    follow_up = (
        await db_session.execute(select(FollowUp).where(FollowUp.tenant_id == tenant.id))
    ).scalar_one()
    assert (await run_due_follow_ups(db_session, now=datetime.now(UTC)))["done"] == 0  # not yet
    await run_due_follow_ups(db_session, now=follow_up.due_at + timedelta(seconds=1))
    assert placed and placed[0]["to"] == "+919800000001" and placed[0]["from_"] == "+919800011111"

    calls = (
        (await db_session.execute(select(Call).where(Call.direction == CallDirection.OUTBOUND)))
        .scalars()
        .all()
    )
    assert len(calls) == 1
    mission = calls[0].mission
    assert (
        mission["callback_of"] == str(call.id)
        and "wants a haircut slot" in mission["goal"]
        and mission["attempt"] == 1
    )

    # Nobody picked up: tried again in 10 minutes, as attempt 2.
    retry = await callbacks.retry_if_unanswered(db_session, calls[0])
    assert retry is not None and retry.payload["attempt"] == 2
    assert retry.payload["reason"] == "wants a haircut slot"
    assert retry.due_at > datetime.now(UTC) + timedelta(minutes=9)

    # Three attempts are the limit.
    calls[0].mission = {**calls[0].mission, "attempt": 3}
    assert await callbacks.retry_if_unanswered(db_session, calls[0]) is None


async def test_an_indian_number_is_called_from_exotel_when_the_workspace_has_one(
    db_session: AsyncSession,
):
    from app.services import exotel

    tenant, _customer, _call = await _setup(db_session)
    db_session.add(
        PhoneNumber(tenant_id=tenant.id, e164_number="+912233445566", provider="exotel")
    )
    await db_session.commit()
    assert (
        await exotel.prefer_indian_cli(db_session, tenant.id, "+919800000001", "+919800011111")
        == "+912233445566"
    )
    assert (
        await exotel.prefer_indian_cli(db_session, tenant.id, "+14155550100", "+919800011111")
        == "+919800011111"
    )


async def test_a_missed_inbound_is_called_back_once_and_a_voicemail_is_not(
    db_session: AsyncSession, monkeypatch
):
    tenant, customer, call = await _setup(db_session)
    missed = MissedCall(
        tenant_id=tenant.id,
        call_id=call.id,
        customer_id=customer.id,
        from_number=customer.phone,
        to_number="+919800011111",
        reason="no-answer",
        created_at=datetime.now(UTC) - timedelta(minutes=2),
    )
    voicemail = MissedCall(
        tenant_id=tenant.id,
        from_number="+919800000099",
        to_number="+919800011111",
        reason="voicemail",
        created_at=datetime.now(UTC) - timedelta(minutes=2),
    )
    db_session.add(missed)
    db_session.add(voicemail)
    await db_session.commit()

    placed: list[dict] = []
    monkeypatch.setattr(
        outbound_call, "place_outbound_call", lambda **kw: placed.append(kw) or "CA-back"
    )
    monkeypatch.setattr(outbound_call, "get_twilio_credentials", _async(None))

    assert await callbacks.recover_missed_calls(db_session) == 1
    assert placed and placed[0]["to"] == customer.phone
    await db_session.refresh(missed)
    assert missed.callback_call_id is not None and missed.recovery_attempted_at is not None
    assert await callbacks.recover_missed_calls(db_session) == 0
    await db_session.refresh(voicemail)
    assert voicemail.callback_call_id is None and voicemail.recovery_attempted_at is None


def _async(value):
    async def f(*a, **k):
        return value

    return f
