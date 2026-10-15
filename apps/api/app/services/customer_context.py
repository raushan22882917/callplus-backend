"""What the AI agent knows about a customer before they say anything, on any
channel: their upcoming bookings and what they said recently elsewhere (a
WhatsApp chat yesterday, a call last week). So "do I have a booking?" is
answered at once, and a caller doesn't have to repeat what they wrote on WhatsApp.
"""

import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import Conversation, ConversationChannel, Message, MessageSender
from packages.database.models.operations import Appointment, AppointmentResource, BookableService

ACTIVE_STATUSES = ("booked", "confirmed", "rescheduled")
RECENT_WINDOW = timedelta(days=7)
CHANNEL_NAMES = {
    ConversationChannel.WHATSAPP: "WhatsApp",
    ConversationChannel.VOICE: "a phone call",
    ConversationChannel.SMS: "SMS",
    ConversationChannel.EMAIL: "email",
    ConversationChannel.WEB_CHAT: "website chat",
}


def _zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def spoken_time(moment: datetime, tz_name: str | None) -> str:
    """'Saturday 4 October, 2:00 pm' in the business's own time zone."""
    local = moment.astimezone(_zone(tz_name))
    hour = local.strftime("%I:%M %p").lstrip("0").lower()
    return f"{local.strftime('%A')} {local.day} {local.strftime('%B')}, {hour}"


async def customer_bookings(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    customer_id: uuid.UUID,
    *,
    include_past_days: int = 0,
    limit: int = 5,
) -> list[dict]:
    since = datetime.now(UTC) - timedelta(days=include_past_days)
    rows = (
        await db.execute(
            select(Appointment, BookableService, AppointmentResource)
            .join(BookableService, BookableService.id == Appointment.service_id)
            .join(AppointmentResource, AppointmentResource.id == Appointment.resource_id)
            .where(
                Appointment.tenant_id == tenant_id,
                Appointment.customer_id == customer_id,
                Appointment.start_at >= since,
            )
            .order_by(Appointment.start_at)
            .limit(limit)
        )
    ).all()
    return [
        {
            "confirmation_id": str(a.id),
            "service": s.name,
            "with": r.name,
            "when": spoken_time(a.start_at, r.timezone),
            "start": a.start_at.astimezone(_zone(r.timezone)).isoformat(),
            "status": a.status,
            "upcoming": a.start_at >= datetime.now(UTC),
        }
        for a, s, r in rows
    ]


def bookings_note(bookings: list[dict]) -> str:
    upcoming = [b for b in bookings if b["upcoming"] and b["status"] in ACTIVE_STATUSES]
    if not upcoming:
        return (
            "\n\nBookings: this customer has no upcoming bookings. If they ask, say so plainly and offer to book one."
        )
    lines = "\n".join(
        f"- {b['service']} with {b['with']}, {b['when']} (confirmation_id {b['confirmation_id']})" for b in upcoming
    )
    return (
        "\n\nThis customer's upcoming bookings (already checked for you; answer from this list when they ask "
        "about their booking, and use the confirmation_id with reschedule_appointment or cancel_appointment, "
        f"never reading the id aloud):\n{lines}"
    )


async def has_bookable_services(db: AsyncSession, tenant_id: uuid.UUID) -> bool:
    found = (
        await db.execute(
            select(BookableService.id)
            .where(BookableService.tenant_id == tenant_id, BookableService.is_active.is_(True))
            .limit(1)
        )
    ).scalar_one_or_none()
    return found is not None


async def other_channel_note(db: AsyncSession, conversation: Conversation, *, limit: int = 6) -> str:
    """The customer's latest messages from their *other* conversations this week."""
    if not conversation.customer_id:
        return ""
    since = datetime.now(UTC) - RECENT_WINDOW
    rows = (
        await db.execute(
            select(Message.sender, Message.text, Message.created_at, Conversation.channel)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Conversation.tenant_id == conversation.tenant_id,
                Conversation.customer_id == conversation.customer_id,
                Conversation.id != conversation.id,
                Conversation.channel != ConversationChannel.INTERNAL,
                Message.created_at >= since,
                Message.text.is_not(None),
            )
            .order_by(Message.created_at.desc())
            .limit(limit)
        )
    ).all()
    if not rows:
        return ""
    lines = []
    for sender, text, created_at, channel in reversed(rows):
        who = "Customer" if sender == MessageSender.CUSTOMER else "Us"
        when = created_at.strftime("%d %b")
        lines.append(f"- {when} on {CHANNEL_NAMES.get(channel, channel.value)}: {who}: {str(text)[:200]}")
    return (
        "\n\nRecent messages with this customer on other channels (context only; mention it naturally if it "
        "helps, e.g. 'I see you asked about this on WhatsApp'):\n" + "\n".join(lines)
    )


async def today_note(db: AsyncSession, tenant_id: uuid.UUID) -> str:
    """The model doesn't know today's date; "tomorrow at 2" needs it."""
    from packages.database.models import Tenant

    tenant = await db.get(Tenant, tenant_id)
    tz_name = tenant.timezone if tenant and tenant.timezone and tenant.timezone != "UTC" else None
    if tz_name is None:
        resource_tz = (
            await db.execute(
                select(AppointmentResource.timezone)
                .where(AppointmentResource.tenant_id == tenant_id, AppointmentResource.is_active.is_(True))
                .limit(1)
            )
        ).scalar_one_or_none()
        tz_name = resource_tz or "Asia/Kolkata"
    now = datetime.now(UTC)
    local = now.astimezone(_zone(tz_name))
    return (
        f"\n\nToday is {spoken_time(now, tz_name)} ({tz_name}). Work out dates like 'tomorrow' or "
        f"'this Saturday' from this; when a tool needs a date, use the format {local.strftime('%Y-%m-%d')}."
    )


async def next_appointment(db: AsyncSession, tenant_id: uuid.UUID, customer_id: uuid.UUID):
    """The customer's next active booking, if any (fills template blanks)."""
    return (
        await db.execute(
            select(Appointment)
            .where(
                Appointment.tenant_id == tenant_id,
                Appointment.customer_id == customer_id,
                Appointment.start_at >= datetime.now(UTC),
                Appointment.status.in_(ACTIVE_STATUSES),
            )
            .order_by(Appointment.start_at)
            .limit(1)
        )
    ).scalar_one_or_none()
