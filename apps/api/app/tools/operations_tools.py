"""AI-agent tools for the operations module. TOOL_SPECS entries have the same
fields as app.tools.registry.ToolSpec (name, description, parameters,
handler) so the registry can do `ToolSpec(**spec)` for each. Handlers only
flush; the agent engine commits the turn."""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from app.services import operations as svc
from app.tools.context import ToolContext
from sqlalchemy import select

from packages.database.models import Customer
from packages.database.models.operations import (
    Appointment,
    AppointmentResource,
    BookableService,
    FollowUp,
)


def _customer_id(ctx: ToolContext, customer_id: str | None) -> uuid.UUID | None:
    if customer_id:
        try:
            return uuid.UUID(customer_id)
        except ValueError:
            return None
    return ctx.customer_id


async def _find_service(ctx: ToolContext, service: str) -> BookableService | None:
    try:
        by_id = await ctx.db.get(BookableService, uuid.UUID(service))
        if by_id is not None and by_id.tenant_id == ctx.tenant_id:
            return by_id
    except ValueError:
        pass
    rows = (
        (
            await ctx.db.execute(
                select(BookableService).where(
                    BookableService.tenant_id == ctx.tenant_id,
                    BookableService.is_active.is_(True),
                    BookableService.name.ilike(f"%{service.strip()}%"),
                )
            )
        )
        .scalars()
        .all()
    )
    exact = [r for r in rows if r.name.lower() == service.strip().lower()]
    if exact:
        return exact[0]
    return rows[0] if len(rows) == 1 else None


async def _find_resource(ctx: ToolContext, resource: str | None) -> tuple[AppointmentResource | None, str | None]:
    """(resource, error). None resource with no error means 'any'."""
    if not resource:
        return None, None
    try:
        by_id = await ctx.db.get(AppointmentResource, uuid.UUID(resource))
        if by_id is not None and by_id.tenant_id == ctx.tenant_id:
            return by_id, None
    except ValueError:
        pass
    rows = (
        (
            await ctx.db.execute(
                select(AppointmentResource).where(
                    AppointmentResource.tenant_id == ctx.tenant_id,
                    AppointmentResource.is_active.is_(True),
                    AppointmentResource.name.ilike(f"%{resource.strip()}%"),
                )
            )
        )
        .scalars()
        .all()
    )
    if len(rows) == 1:
        return rows[0], None
    return None, f"could not find a single resource matching {resource!r}"


async def _parse_start(ctx: ToolContext, value: str, resource: AppointmentResource | None) -> datetime:
    """ISO datetime; a time without offset is read in the resource's timezone
    (or the first active resource's, or UTC)."""
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        if resource is None:
            resource = (
                (
                    await ctx.db.execute(
                        select(AppointmentResource)
                        .where(
                            AppointmentResource.tenant_id == ctx.tenant_id,
                            AppointmentResource.is_active.is_(True),
                        )
                        .order_by(AppointmentResource.name)
                    )
                )
                .scalars()
                .first()
            )
        parsed = parsed.replace(tzinfo=ZoneInfo(resource.timezone if resource else "UTC"))
    return parsed.astimezone(UTC)


def _summary(a: Appointment, service: BookableService, resource: AppointmentResource) -> dict:
    local = a.start_at.astimezone(ZoneInfo(resource.timezone))
    return {
        "confirmation_id": str(a.id),
        "service": service.name,
        "resource": resource.name,
        "start": local.isoformat(),
        "timezone": resource.timezone,
        "status": a.status,
    }


async def _notify(ctx: ToolContext, appointment: Appointment, purpose: str) -> bool:
    """WhatsApp the customer the business's approved template for this change."""
    from app.services.whatsapp_templates import notify_booking

    await ctx.db.flush()
    return await notify_booking(ctx.db, ctx.tenant_id, appointment, purpose)


async def check_availability(
    ctx: ToolContext, *, service: str, date: str, resource: str | None = None
) -> dict:
    svc_row = await _find_service(ctx, service)
    if svc_row is None:
        return {"error": f"no unique bookable service matches {service!r}"}
    res, error = await _find_resource(ctx, resource)
    if error:
        return {"error": error}
    try:
        day = _date(date)
        slots = await svc.free_slots(ctx.db, ctx.tenant_id, day, svc_row.id, res.id if res else None)
    except (ValueError, svc.AppointmentError) as exc:
        return {"error": str(exc)}
    tzs = {
        r.id: ZoneInfo(r.timezone)
        for r in (
            await ctx.db.execute(
                select(AppointmentResource).where(AppointmentResource.tenant_id == ctx.tenant_id)
            )
        ).scalars()
    }
    return {
        "service": svc_row.name,
        "duration_minutes": svc_row.duration_minutes,
        "date": day.isoformat(),
        "slots": [
            {
                "start": s["start"].astimezone(tzs[s["resource_id"]]).strftime("%H:%M"),
                "resource": s["resource_name"],
            }
            for s in slots[:40]
        ],
        "total_free": len(slots),
    }


def _date(value: str) -> date:
    return date.fromisoformat(value.strip()[:10])


async def book_appointment(
    ctx: ToolContext,
    *,
    service: str,
    start_time: str,
    resource: str | None = None,
    notes: str | None = None,
    customer_id: str | None = None,
    caller_name: str | None = None,
    contact_phone: str | None = None,
) -> dict:
    from app.tools.handlers import _other_number, remember_caller_name

    target = _customer_id(ctx, customer_id)
    if target is None:
        return {"error": "no customer on this conversation"}
    await remember_caller_name(ctx, target, caller_name)
    owner = await ctx.db.get(Customer, target)
    if other := _other_number(contact_phone, owner.phone if owner else None):
        notes = f"{notes or ''} Contact number: {other}".strip()
    svc_row = await _find_service(ctx, service)
    if svc_row is None:
        return {"error": f"no unique bookable service matches {service!r}"}
    res, error = await _find_resource(ctx, resource)
    if error:
        return {"error": error}
    try:
        start = await _parse_start(ctx, start_time, res)
        appointment = await svc.book_appointment(
            ctx.db,
            ctx.tenant_id,
            customer_id=target,
            service_id=svc_row.id,
            start_at=start,
            resource_id=res.id if res else None,
            source="ai_agent",
            notes=notes,
        )
    except (ValueError, svc.AppointmentError) as exc:
        return {"error": str(exc), "booked": False}
    booked_resource = await ctx.db.get(AppointmentResource, appointment.resource_id)
    notified = await _notify(ctx, appointment, "booking_confirmation")
    return {"booked": True, "whatsapp_confirmation_sent": notified, **_summary(appointment, svc_row, booked_resource)}


async def _owned_appointment(
    ctx: ToolContext, appointment_id: str
) -> tuple[Appointment | None, str | None]:
    try:
        appointment = await svc.get_appointment(ctx.db, ctx.tenant_id, uuid.UUID(appointment_id))
    except (ValueError, svc.AppointmentError):
        return None, "appointment not found"
    if ctx.customer_id is not None and appointment.customer_id != ctx.customer_id:
        return None, "appointment not found"
    return appointment, None


async def reschedule_appointment(
    ctx: ToolContext, *, appointment_id: str, new_start_time: str, resource: str | None = None
) -> dict:
    appointment, error = await _owned_appointment(ctx, appointment_id)
    if error:
        return {"error": error}
    res, error = await _find_resource(ctx, resource)
    if error:
        return {"error": error}
    try:
        current = await ctx.db.get(AppointmentResource, appointment.resource_id)
        start = await _parse_start(ctx, new_start_time, res or current)
        await svc.reschedule_appointment(
            ctx.db, ctx.tenant_id, appointment.id, start_at=start,
            resource_id=res.id if res else None,
        )
    except (ValueError, svc.AppointmentError) as exc:
        return {"error": str(exc), "rescheduled": False}
    service = await ctx.db.get(BookableService, appointment.service_id)
    new_resource = await ctx.db.get(AppointmentResource, appointment.resource_id)
    notified = await _notify(ctx, appointment, "booking_rescheduled")
    return {"rescheduled": True, "whatsapp_confirmation_sent": notified, **_summary(appointment, service, new_resource)}


async def cancel_appointment(
    ctx: ToolContext, *, appointment_id: str, reason: str | None = None
) -> dict:
    appointment, error = await _owned_appointment(ctx, appointment_id)
    if error:
        return {"error": error}
    try:
        await svc.cancel_appointment(ctx.db, ctx.tenant_id, appointment.id, reason)
    except svc.AppointmentError as exc:
        return {"error": str(exc), "cancelled": False}
    notified = await _notify(ctx, appointment, "booking_cancelled")
    return {"cancelled": True, "confirmation_id": str(appointment.id), "whatsapp_confirmation_sent": notified}


async def list_my_appointments(
    ctx: ToolContext, *, include_past: bool = False, customer_id: str | None = None
) -> dict:
    """The caller's bookings (upcoming, plus the last 30 days if include_past)."""
    from app.services.customer_context import customer_bookings

    target = _customer_id(ctx, customer_id)
    if target is None:
        return {"error": "no customer on this conversation"}
    bookings = await customer_bookings(
        ctx.db, ctx.tenant_id, target, include_past_days=30 if include_past else 0, limit=10
    )
    return {"appointments": bookings, "count": len(bookings)}


async def schedule_follow_up(
    ctx: ToolContext,
    *,
    channel: str,
    due_at: str,
    message: str = "",
    customer_id: str | None = None,
) -> dict:
    if channel not in ("call", "whatsapp", "sms", "email"):
        return {"error": "channel must be call, whatsapp, sms or email"}
    target = _customer_id(ctx, customer_id)
    if target is None:
        return {"error": "no customer on this conversation"}
    customer = await ctx.db.get(Customer, target)
    if customer is None or customer.tenant_id != ctx.tenant_id:
        return {"error": "customer not found"}
    if channel != "call" and not message.strip():
        return {"error": "message is required for this channel"}
    try:
        due = datetime.fromisoformat(due_at.strip().replace("Z", "+00:00"))
    except ValueError:
        return {"error": "due_at must be an ISO 8601 datetime"}
    if due.tzinfo is None:
        due = due.replace(tzinfo=UTC)
    follow_up = FollowUp(
        tenant_id=ctx.tenant_id,
        customer_id=target,
        channel=channel,
        due_at=due.astimezone(UTC),
        payload={"message": message.strip()} if message.strip() else {},
        created_by="ai",
    )
    ctx.db.add(follow_up)
    await ctx.db.flush()
    return {"scheduled": True, "follow_up_id": str(follow_up.id), "due_at": follow_up.due_at.isoformat()}


async def record_promise_to_pay(
    ctx: ToolContext,
    *,
    amount: float,
    due_date: str,
    currency: str = "INR",
    notes: str | None = None,
    customer_id: str | None = None,
) -> dict:
    target = _customer_id(ctx, customer_id)
    if target is None:
        return {"error": "no customer on this conversation"}
    try:
        value = Decimal(str(amount))
        due = _date(due_date)
    except (InvalidOperation, ValueError):
        return {"error": "amount must be a number and due_date a YYYY-MM-DD date"}
    if value <= 0:
        return {"error": "amount must be positive"}
    if due < datetime.now(UTC).date():
        return {"error": "due_date cannot be in the past"}
    try:
        promise, reminder = await svc.create_payment_promise(
            ctx.db,
            ctx.tenant_id,
            customer_id=target,
            amount=value,
            due_date=due,
            currency=currency,
            notes=notes,
            created_by="ai",
            call_id=ctx.call_id,
        )
    except ValueError as exc:
        return {"error": str(exc)}
    return {
        "recorded": True,
        "promise_id": str(promise.id),
        "amount": str(promise.amount),
        "due_date": promise.due_date.isoformat(),
        "reminder_scheduled": reminder is not None,
    }


TOOL_SPECS: dict[str, dict] = {
    "list_my_appointments": {
        "name": "list_my_appointments",
        "description": "List the caller's bookings: service, staff, day and time, status and "
        "confirmation_id. Upcoming ones are already in your instructions; use this for past or "
        "cancelled bookings (include_past=true).",
        "parameters": {
            "type": "object",
            "properties": {
                "include_past": {"type": "boolean", "default": False},
            },
        },
        "handler": list_my_appointments,
    },
    "check_availability": {
        "name": "check_availability",
        "description": "List free appointment slots for a service on a date. Call this "
        "before offering times; only offer slots it returns.",
        "parameters": {
            "type": "object",
            "properties": {
                "service": {"type": "string", "description": "Service name (or id)"},
                "date": {"type": "string", "description": "YYYY-MM-DD"},
                "resource": {"type": "string", "description": "Optional staff/room name"},
            },
            "required": ["service", "date"],
        },
        "handler": check_availability,
    },
    "book_appointment": {
        "name": "book_appointment",
        "description": "Book an appointment for the caller at a free slot. Returns a "
        "confirmation_id (keep it; never read it aloud). If it errors, the slot was not booked.",
        "parameters": {
            "type": "object",
            "properties": {
                "service": {"type": "string"},
                "start_time": {"type": "string", "description": "ISO 8601 local start, e.g. 2026-10-05T15:30"},
                "resource": {"type": "string", "description": "Optional staff/room name"},
                "notes": {"type": "string"},
                "caller_name": {"type": "string", "description": "The caller's name, if they told you. Saved on their record."},
                "contact_phone": {
                    "type": "string",
                    "description": "Only if they want to be reached on a different number than the one they're using.",
                },
            },
            "required": ["service", "start_time"],
        },
        "handler": book_appointment,
    },
    "reschedule_appointment": {
        "name": "reschedule_appointment",
        "description": "Move one of the caller's appointments to a new free time.",
        "parameters": {
            "type": "object",
            "properties": {
                "appointment_id": {"type": "string", "description": "The confirmation_id"},
                "new_start_time": {"type": "string", "description": "ISO 8601 local start"},
                "resource": {"type": "string"},
            },
            "required": ["appointment_id", "new_start_time"],
        },
        "handler": reschedule_appointment,
    },
    "cancel_appointment": {
        "name": "cancel_appointment",
        "description": "Cancel one of the caller's appointments.",
        "parameters": {
            "type": "object",
            "properties": {
                "appointment_id": {"type": "string", "description": "The confirmation_id"},
                "reason": {"type": "string"},
            },
            "required": ["appointment_id"],
        },
        "handler": cancel_appointment,
    },
    "schedule_follow_up": {
        "name": "schedule_follow_up",
        "description": "Schedule a future follow-up to the customer by call, WhatsApp, "
        "SMS or email at a specific time.",
        "parameters": {
            "type": "object",
            "properties": {
                "channel": {"type": "string", "enum": ["call", "whatsapp", "sms", "email"]},
                "due_at": {"type": "string", "description": "ISO 8601 datetime (UTC if no offset)"},
                "message": {"type": "string", "description": "Text to send (not needed for call)"},
                "customer_id": {"type": "string"},
            },
            "required": ["channel", "due_at"],
        },
        "handler": schedule_follow_up,
    },
    "record_promise_to_pay": {
        "name": "record_promise_to_pay",
        "description": "Record that the customer promised to pay an amount by a date. "
        "A reminder is scheduled automatically.",
        "parameters": {
            "type": "object",
            "properties": {
                "amount": {"type": "number"},
                "due_date": {"type": "string", "description": "YYYY-MM-DD"},
                "currency": {"type": "string", "default": "INR"},
                "notes": {"type": "string"},
                "customer_id": {"type": "string"},
            },
            "required": ["amount", "due_date"],
        },
        "handler": record_promise_to_pay,
    },
}
