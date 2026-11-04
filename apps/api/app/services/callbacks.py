"""Call the person back when they ask: "call me in 5 minutes", "tomorrow at 4 PM".

During a call the AI works out the time (it is told the current time and the business's time zone),
and `schedule_callback` stores it as a call follow-up. The minute-by-minute follow-up runner then
places an AI call at that time, from the same business number, with the same agent, knowing why it is
calling. A callback nobody answers is tried again a little later, up to three times.
"""

import re
import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import Call, FollowUp

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
