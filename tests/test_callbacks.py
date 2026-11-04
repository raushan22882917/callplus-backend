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
    PhoneNumber,
    Tenant,
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


def _async(value):
    async def f(*a, **k):
        return value

    return f
