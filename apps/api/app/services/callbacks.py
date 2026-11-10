"""Call the person back when they ask: "call me in 5 minutes", "tomorrow at 4 PM".

During a call the AI works out the time (it is told the current time and the business's time zone),
and `schedule_callback` stores it as a call follow-up. The minute-by-minute follow-up runner then
places an AI call at that time, from the same business number, with the same agent, knowing why it is
calling. A callback nobody answers is tried again a little later, up to three times.
"""

import logging
import re
import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import Call, Customer, FollowUp, Tenant, Ticket

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
RETRY_AFTER = timedelta(minutes=10)
MAX_AHEAD = timedelta(days=30)
_UNITS = {"s": 1, "sec": 1, "second": 1, "m": 60, "min": 60, "minute": 60, "h": 3600, "hr": 3600,
          "hour": 3600, "d": 86400, "day": 86400}
_RELATIVE = re.compile(
    r"\bin\s+(?P<n>\d+(?:\.\d+)?|a|an|half an?|one|two|three|four|five|ten|fifteen|twenty|thirty)\s*"
    r"(?P<u>seconds?|secs?|minutes?|mins?|hours?|hrs?|days?)\b",
    re.I,
)
_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "ten": 10,
          "fifteen": 15, "twenty": 20, "thirty": 30}
_CLOCK = re.compile(r"\b(?:at\s*)?(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?\b", re.I)


def tz_for(name: str | None) -> ZoneInfo:
    """New workspaces default to UTC; nearly all are in India, so UTC counts as "not set"."""
    try:
        return ZoneInfo("Asia/Kolkata" if name in (None, "", "UTC") else name)
    except Exception:
        return ZoneInfo("Asia/Kolkata")


def parse_when(
    text: str | None,
    *,
    minutes_from_now: int | None = None,
    now: datetime | None = None,
    tz: ZoneInfo | None = None,
) -> datetime | None:
    """The moment to call, in UTC, or None if it cannot be worked out (or is out of range)."""
    tz = tz or tz_for(None)
    now = (now or datetime.now(UTC)).astimezone(UTC)
    local_now = now.astimezone(tz)
    due: datetime | None = None

    if minutes_from_now is not None:
        due = now + timedelta(minutes=int(minutes_from_now))
    elif text:
        t = text.strip()
        m = _RELATIVE.search(t)
        if m:
            raw = m.group("n").lower()
            amount = 0.5 if raw.startswith("half") else float(_WORDS.get(raw, raw))
            unit = m.group("u").lower().rstrip("s")
            due = now + timedelta(seconds=amount * _UNITS.get(unit, 60))
        else:
            try:  # "2026-11-05 16:00" or an ISO time with an offset
                parsed = datetime.fromisoformat(t.replace("Z", "+00:00"))
                due = (parsed if parsed.tzinfo else parsed.replace(tzinfo=tz)).astimezone(UTC)
            except ValueError:
                due = _clock_time(t, local_now)
    if due is None:
        return None
    if due < now + timedelta(seconds=30):
        due = now + timedelta(seconds=30)  # "right now" still means a moment from now
    if due > now + MAX_AHEAD:
        return None
    return due


def _clock_time(text: str, local_now: datetime) -> datetime | None:
    """"4 pm", "tomorrow at 10:30", "today 17:00": a time of day on today or tomorrow, business time."""
    lowered = text.lower()
    m = _CLOCK.search(lowered)
    if not m:
        return None
    hour, minute, meridiem = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").replace(".", "")
    if not (meridiem or m.group(2) or "at" in lowered):
        return None  # a bare number could be anything
    if meridiem == "pm" and hour < 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    day = local_now.date() + (timedelta(days=1) if "tomorrow" in lowered or "kal" in lowered else timedelta())
    due = datetime(day.year, day.month, day.day, hour, minute, tzinfo=local_now.tzinfo)
    if due <= local_now and "tomorrow" not in lowered:
        if not meridiem and hour < 12:  # "at 4" said in the afternoon means the next 4
            due = due + timedelta(hours=12) if due + timedelta(hours=12) > local_now else due + timedelta(days=1)
        else:
            due = due + timedelta(days=1)
    return due.astimezone(UTC)


_CALLBACK_PREFIX = "callback requested for "


def when_from_callback_subject(
    subject: str, created_at: datetime, tz: ZoneInfo
) -> tuple[datetime, str] | None:
    """The time a "Callback requested for today at 4 PM — …" ticket is asking for.

    "today" and "tomorrow" are read from the moment the ticket was written, in the
    business time zone, which is what the caller meant."""
    if not subject.lower().startswith(_CALLBACK_PREFIX):
        return None
    rest = subject[len(_CALLBACK_PREFIX) :]
    note = ""
    if " — " in rest:
        rest, note = rest.split(" — ", 1)
    rest = re.sub(r"\s+on\s+\+[\d\s-]+$", "", rest).strip()
    due = parse_when(rest, now=created_at, tz=tz)
    if due is None:
        return None
    return due, note.strip()


def describe(due: datetime, tz: ZoneInfo, now: datetime | None = None) -> str:
    """How the AI says it back to the caller: "in about 5 minutes" or "tomorrow at 4:00 PM"."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    gap = due - now
    if gap < timedelta(minutes=1):
        return "in a moment"
    if gap < timedelta(hours=1):
        return f"in about {round(gap.total_seconds() / 60)} minutes"
    local = due.astimezone(tz)
    today = now.astimezone(tz).date()
    day = "today" if local.date() == today else "tomorrow" if local.date() == today + timedelta(days=1) else local.strftime("%A %d %B")
    return f"{day} at {local.strftime('%I:%M %p').lstrip('0')}"


async def schedule(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    customer_id: uuid.UUID,
    due_at: datetime,
    call: Call | None,
    reason: str = "",
    phone: str | None = None,
    attempt: int = 1,
    created_by: str = "ai",
) -> FollowUp:
    """One pending callback per call: asking again ("make it 10 minutes") moves it."""
    if call is not None:
        existing = (
            await db.execute(
                select(FollowUp).where(
                    FollowUp.tenant_id == tenant_id,
                    FollowUp.channel == "call",
                    FollowUp.status == "pending",
                    FollowUp.reference_type == "call",
                    FollowUp.reference_id == call.id,
                )
            )
        ).scalars().first()
        if existing is not None:
            existing.due_at = due_at
            existing.payload = {**existing.payload, "reason": reason or existing.payload.get("reason", "")}
            return existing

    # Call back from the business number they reached; for an outbound call, the number we used.
    from_number = None
    if call is not None:
        from_number = call.to_number if call.direction.value == "inbound" else call.from_number
    follow_up = FollowUp(
        tenant_id=tenant_id,
        customer_id=customer_id,
        channel="call",
        due_at=due_at,
        created_by=created_by,
        reference_type="call" if call is not None else None,
        reference_id=call.id if call is not None else None,
        payload={
            "ai_agent_id": str(call.ai_agent_id) if call is not None and call.ai_agent_id else None,
            "from_number": from_number,
            "reason": reason,
            "callback_of": str(call.id) if call is not None else None,
            "attempt": attempt,
            "to_number": phone,
        },
    )
    db.add(follow_up)
    await db.flush()
    return follow_up


async def calendar_callbacks(
    db: AsyncSession, tenant_id: uuid.UUID, start: datetime, end: datetime
) -> dict:
    """Callback tickets and scheduled call-backs whose time falls in [start, end).

    When the window is empty, `nearest_at` is the closest such time outside it,
    so the calendar can jump to the week that actually has requests."""
    tenant = await db.get(Tenant, tenant_id)
    tz = tz_for(tenant.timezone if tenant else None)
    start = start.astimezone(UTC)
    end = end.astimezone(UTC)
    found: list[dict] = []

    tickets = (
        await db.execute(
            select(Ticket, Customer.name)
            .join(Customer, Customer.id == Ticket.customer_id)
            .where(
                Ticket.tenant_id == tenant_id,
                Ticket.subject.ilike("Callback requested for %"),
                Ticket.created_at >= start - timedelta(days=120),
                Ticket.created_at < end + timedelta(days=1),
            )
            .order_by(Ticket.created_at.desc())
            .limit(300)
        )
    ).all()
    for ticket, name in tickets:
        parsed = when_from_callback_subject(ticket.subject, ticket.created_at, tz)
        if parsed is None:
            continue
        due, note = parsed
        found.append(
            {
                "id": ticket.id,
                "kind": "ticket",
                "start_at": due,
                "end_at": due + timedelta(minutes=30),
                "title": name or "Customer",
                "detail": note or "Callback",
                "customer_name": name,
                "ticket_id": ticket.id,
            }
        )

    follow_ups = (
        await db.execute(
            select(FollowUp, Customer.name)
            .join(Customer, Customer.id == FollowUp.customer_id)
            .where(
                FollowUp.tenant_id == tenant_id,
                FollowUp.channel == "call",
                FollowUp.status != "cancelled",
                FollowUp.due_at >= start - timedelta(days=120),
                FollowUp.due_at < end + timedelta(days=30),
            )
        )
    ).all()
    for follow_up, name in follow_ups:
        reason = (follow_up.payload or {}).get("reason") or "Call back"
        found.append(
            {
                "id": follow_up.id,
                "kind": "follow_up",
                "start_at": follow_up.due_at,
                "end_at": follow_up.due_at + timedelta(minutes=15),
                "title": name or "Customer",
                "detail": reason,
                "customer_name": name,
                "ticket_id": None,
            }
        )

    inside = [e for e in found if start <= e["start_at"] < end]
    inside.sort(key=lambda e: e["start_at"])
    outside = [e for e in found if e["start_at"] < start or e["start_at"] >= end]
    nearest = None
    if not inside and outside:
        nearest = min(outside, key=lambda e: abs((e["start_at"] - start).total_seconds()))["start_at"]
    return {"events": inside, "nearest_at": nearest}


def mission_for(payload: dict) -> dict:
    reason = (payload.get("reason") or "").strip()
    goal = "You are calling them back because they asked you to call at this time."
    if reason:
        goal += f" What they wanted: {reason}"
    return {
        "goal": goal,
        "questions": [],
        "callback_of": payload.get("callback_of"),
        "attempt": int(payload.get("attempt") or 1),
    }


async def retry_if_unanswered(db: AsyncSession, call: Call) -> FollowUp | None:
    """A callback that nobody picked up is tried again in 10 minutes, up to 3 attempts in all."""
    mission = call.mission or {}
    if not mission.get("callback_of") or call.customer_id is None:
        return None
    attempt = int(mission.get("attempt") or 1)
    if attempt >= MAX_ATTEMPTS:
        return None
    origin = await db.get(Call, uuid.UUID(mission["callback_of"]))
    follow_up = FollowUp(
        tenant_id=call.tenant_id,
        customer_id=call.customer_id,
        channel="call",
        due_at=datetime.now(UTC) + RETRY_AFTER,
        created_by="ai",
        reference_type="call",
        reference_id=call.id,
        payload={
            "ai_agent_id": str(call.ai_agent_id) if call.ai_agent_id else None,
            "from_number": call.from_number,
            "reason": (mission.get("goal") or "").split("What they wanted: ")[-1]
            if "What they wanted: " in (mission.get("goal") or "")
            else "",
            "callback_of": str(origin.id) if origin else mission["callback_of"],
            "attempt": attempt + 1,
            "to_number": call.to_number,
        },
    )
    db.add(follow_up)
    await db.flush()
    return follow_up


async def _whatsapp_after_miss(
    db: AsyncSession, tenant_id: uuid.UUID, phone: str, customer_id
) -> None:
    """Send the missed-call WhatsApp template when the callback cannot be placed."""
    from app.services.whatsapp_templates import context_values, find_approved, send_template

    from packages.database.models import Customer

    template = await find_approved(db, tenant_id, purpose="missed_call")
    if template is None:
        return
    customer = await db.get(Customer, customer_id) if customer_id else None
    try:
        values = await context_values(db, tenant_id, customer)
        await send_template(db, tenant_id, to=phone, template=template, values=values)
    except Exception as exc:  # noqa: BLE001
        logger.warning("missed-call WhatsApp not sent: %s", exc)


async def recover_missed_calls(db: AsyncSession) -> int:
    """Call back an inbound call nobody answered, once, about a minute later.

    A voicemail is left alone: they already left a message. A callback that
    itself was missed is not called again. If the call cannot be placed, the
    approved missed-call WhatsApp template is sent instead.
    """
    from app.services.outbound_call import OutboundCallError, place_ai_outbound_call

    from packages.database.models import Call, MissedCall

    now = datetime.now(UTC)
    due = now - timedelta(seconds=45)
    rows = list(
        (
            await db.execute(
                select(MissedCall)
                .where(
                    MissedCall.handled.is_(False),
                    MissedCall.callback_call_id.is_(None),
                    MissedCall.recovery_attempted_at.is_(None),
                    MissedCall.reason != "voicemail",
                    MissedCall.created_at <= due,
                    MissedCall.from_number.is_not(None),
                )
                .limit(20)
            )
        )
        .scalars()
        .all()
    )
    recovered = 0
    for missed in rows:
        agent_id = None
        if missed.call_id:
            origin = await db.get(Call, missed.call_id)
            if origin is not None and (origin.mission or {}).get("callback_of"):
                missed.recovery_attempted_at = now
                continue
            if origin is not None and origin.ai_agent_id is not None:
                agent_id = str(origin.ai_agent_id)
        missed_id = missed.id
        tenant_id = missed.tenant_id
        phone = missed.from_number or ""
        caller_id = missed.to_number
        customer_id = missed.customer_id
        missed.recovery_attempted_at = now
        await db.commit()
        try:
            call = await place_ai_outbound_call(
                db,
                tenant_id,
                to_number=phone,
                from_number=caller_id,
                ai_agent_id=agent_id,
                mission={"callback_of": str(missed_id), "reason": "missed inbound"},
            )
        except OutboundCallError as exc:
            logger.info("missed-call callback not placed: %s", exc)
            await db.rollback()
            await _whatsapp_after_miss(db, tenant_id, phone, customer_id)
            continue
        fresh = await db.get(MissedCall, missed_id)
        if fresh is not None:
            fresh.callback_call_id = call.id
            await db.commit()
            recovered += 1
    await db.commit()
    return recovered
