"""Operations business logic: SLA clocks, appointment availability/booking,
follow-up execution and payment promises. Router, Celery tasks and AI tool
handlers all call into this module so the rules live in one place."""

import html
import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core.config import get_settings
from app.integrations.email_client import send_email
from app.integrations.whatsapp import WhatsAppAPIError, send_text_message
from app.services import calendar_sync, plans
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import Customer, PhoneNumber, Ticket, TicketComment, WhatsAppAccount
from packages.database.models.operations import (
    Appointment,
    AppointmentResource,
    BookableService,
    FollowUp,
    PaymentPromise,
    SLAPolicy,
    TicketCategory,
    TicketSLA,
)

logger = logging.getLogger(__name__)

ACTIVE_APPOINTMENT_STATUSES = ("booked", "confirmed")
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
REMINDER_LEAD = timedelta(hours=24)
MAX_REMINDER_ATTEMPTS = 3
MAX_FOLLOW_UP_ATTEMPTS = 3
FOLLOW_UP_RETRY_DELAY = timedelta(minutes=10)
AT_RISK_FRACTION = 0.25


def _tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown timezone {name!r}") from exc


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


# --------------------------------------------------------------------------
# SLA
# --------------------------------------------------------------------------


def add_business_minutes(
    start: datetime, minutes: int, *, start_hour: int, end_hour: int, tz_name: str = "UTC"
) -> datetime:
    """`start` + `minutes` counting only Mon-Fri, start_hour..end_hour local
    time. Returned in UTC."""
    if not 0 <= start_hour < end_hour <= 24:
        raise ValueError("business hours must satisfy 0 <= start < end <= 24")
    tz = _tz(tz_name)
    cur = _aware(start).astimezone(tz)
    remaining = timedelta(minutes=minutes)

    def day_bounds(d: datetime) -> tuple[datetime, datetime]:
        midnight = d.replace(hour=0, minute=0, second=0, microsecond=0)
        return midnight + timedelta(hours=start_hour), midnight + timedelta(hours=end_hour)

    for _ in range(10_000):
        day_start, day_end = day_bounds(cur)
        if cur.weekday() >= 5 or cur >= day_end:
            cur = day_start + timedelta(days=1)
            continue
        if cur < day_start:
            cur = day_start
        available = day_end - cur
        if remaining <= available:
            return (cur + remaining).astimezone(UTC)
        remaining -= available
        cur = day_start + timedelta(days=1)
    raise ValueError("could not place SLA deadline")


def compute_due(
    start: datetime,
    minutes: int,
    *,
    business_hours_only: bool,
    start_hour: int = 9,
    end_hour: int = 18,
    tz_name: str = "UTC",
) -> datetime:
    if not business_hours_only:
        return _aware(start).astimezone(UTC) + timedelta(minutes=minutes)
    return add_business_minutes(
        start, minutes, start_hour=start_hour, end_hour=end_hour, tz_name=tz_name
    )


def sla_clock(
    started_at: datetime, due_at: datetime, done_at: datetime | None, now: datetime
) -> dict:
    """state: met | breached | at_risk | ok. seconds_remaining is negative once
    overdue and None once the clock has stopped."""
    started_at, due_at, now = _aware(started_at), _aware(due_at), _aware(now)
    if done_at is not None:
        done_at = _aware(done_at)
        return {"state": "met" if done_at <= due_at else "breached", "seconds_remaining": None}
    remaining = (due_at - now).total_seconds()
    if remaining < 0:
        return {"state": "breached", "seconds_remaining": int(remaining)}
    window = (due_at - started_at).total_seconds()
    state = "at_risk" if window > 0 and remaining <= window * AT_RISK_FRACTION else "ok"
    return {"state": state, "seconds_remaining": int(remaining)}


def policy_due_times(policy: SLAPolicy, start: datetime) -> tuple[datetime, datetime]:
    kwargs = {
        "business_hours_only": policy.business_hours_only,
        "start_hour": policy.business_start_hour,
        "end_hour": policy.business_end_hour,
        "tz_name": policy.timezone,
    }
    return (
        compute_due(start, policy.first_response_minutes, **kwargs),
        compute_due(start, policy.resolution_minutes, **kwargs),
    )


async def apply_sla(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    ticket: Ticket,
    *,
    category_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> TicketSLA | None:
    """Attach an SLA clock to a ticket from the tenant's policy for its
    priority. Returns None when no active policy exists. Flushes only."""
    existing = (
        await db.execute(select(TicketSLA).where(TicketSLA.ticket_id == ticket.id))
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    priority = ticket.priority.value if hasattr(ticket.priority, "value") else str(ticket.priority)
    policy = (
        await db.execute(
            select(SLAPolicy).where(
                SLAPolicy.tenant_id == tenant_id,
                SLAPolicy.priority == priority,
                SLAPolicy.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if policy is None:
        return None
    department = None
    if category_id is not None:
        category = await db.get(TicketCategory, category_id)
        if category is None or category.tenant_id != tenant_id:
            category_id = None
        else:
            department = category.department
    started = _aware(now or datetime.now(UTC))
    first_due, resolution_due = policy_due_times(policy, started)
    sla = TicketSLA(
        tenant_id=tenant_id,
        ticket_id=ticket.id,
        policy_id=policy.id,
        category_id=category_id,
        department=department,
        priority=priority,
        started_at=started,
        first_response_due_at=first_due,
        resolution_due_at=resolution_due,
    )
    db.add(sla)
    await db.flush()
    return sla


async def reapply_sla_for_priority(db: AsyncSession, tenant_id: uuid.UUID, ticket: Ticket) -> None:
    """A ticket whose priority changed gets the new priority's targets, measured from
    when the ticket was opened. Resolved tickets and tickets without an SLA are left
    alone; a breach already recorded stays recorded."""
    try:
        async with db.begin_nested():
            sla = (
                await db.execute(
                    select(TicketSLA).where(
                        TicketSLA.tenant_id == tenant_id, TicketSLA.ticket_id == ticket.id
                    )
                )
            ).scalar_one_or_none()
            priority = ticket.priority.value if hasattr(ticket.priority, "value") else str(ticket.priority)
            if sla is None or sla.resolved_at is not None or sla.priority == priority:
                return
            policy = (
                await db.execute(
                    select(SLAPolicy).where(
                        SLAPolicy.tenant_id == tenant_id,
                        SLAPolicy.priority == priority,
                        SLAPolicy.is_active.is_(True),
                    )
                )
            ).scalar_one_or_none()
            if policy is None:
                return
            first_due, resolution_due = policy_due_times(policy, _aware(sla.started_at))
            sla.priority = priority
            sla.policy_id = policy.id
            if sla.first_responded_at is None:
                sla.first_response_due_at = first_due
            sla.resolution_due_at = resolution_due
            await db.flush()
    except Exception:
        logger.warning("could not re-apply SLA to ticket %s", ticket.id, exc_info=True)


async def apply_sla_safely(
    db: AsyncSession, tenant_id: uuid.UUID, ticket: Ticket, **kwargs
) -> TicketSLA | None:
    """Ticket creation must never fail because of SLA bookkeeping: run in a
    savepoint and swallow (and log) any error."""
    try:
        async with db.begin_nested():
            return await apply_sla(db, tenant_id, ticket, **kwargs)
    except Exception:
        logger.warning("could not apply SLA to ticket %s", ticket.id, exc_info=True)
        return None


@dataclass
class SLABreach:
    tenant_id: uuid.UUID
    ticket_id: uuid.UUID
    kind: str  # first_response | resolution
    priority: str
    department: str | None
    due_at: datetime


async def refresh_sla(
    db: AsyncSession, *, tenant_id: uuid.UUID | None = None, now: datetime | None = None
) -> list[SLABreach]:
    """Advance every running SLA clock (first response, resolution) and flag
    new breaches. Flushes; the caller commits and emits `sla.breached`."""
    now = _aware(now or datetime.now(UTC))
    query = (
        select(TicketSLA, Ticket)
        .join(Ticket, Ticket.id == TicketSLA.ticket_id)
        .where(TicketSLA.resolved_at.is_(None))
    )
    if tenant_id is not None:
        query = query.where(TicketSLA.tenant_id == tenant_id)
    rows = (await db.execute(query)).all()
    if not rows:
        return []

    pending_ids = [sla.ticket_id for sla, _ in rows if sla.first_responded_at is None]
    first_comment: dict[uuid.UUID, datetime] = {}
    if pending_ids:
        from sqlalchemy import func

        comment_rows = await db.execute(
            select(TicketComment.ticket_id, func.min(TicketComment.created_at))
            .where(
                TicketComment.ticket_id.in_(pending_ids), TicketComment.author_user_id.is_not(None)
            )
            .group_by(TicketComment.ticket_id)
        )
        first_comment = {tid: ts for tid, ts in comment_rows.all()}

    breaches: list[SLABreach] = []
    for sla, ticket in rows:
        if sla.first_responded_at is None and sla.ticket_id in first_comment:
            sla.first_responded_at = first_comment[sla.ticket_id]
        if ticket.status.value in ("resolved", "closed"):
            sla.resolved_at = _aware(ticket.updated_at)
            if sla.first_responded_at is None:
                sla.first_responded_at = sla.resolved_at

        def check(done_at: datetime | None, due: datetime) -> bool:
            return _aware(done_at or now) > _aware(due)

        if not sla.first_response_breached and check(
            sla.first_responded_at, sla.first_response_due_at
        ):
            sla.first_response_breached = True
            breaches.append(
                SLABreach(sla.tenant_id, sla.ticket_id, "first_response", sla.priority,
                          sla.department, sla.first_response_due_at)
            )
        if not sla.resolution_breached and check(sla.resolved_at, sla.resolution_due_at):
            sla.resolution_breached = True
            breaches.append(
                SLABreach(sla.tenant_id, sla.ticket_id, "resolution", sla.priority,
                          sla.department, sla.resolution_due_at)
            )
        if (sla.first_response_breached or sla.resolution_breached) and sla.breach_notified_at is None:
            sla.breach_notified_at = now
    await db.flush()
    return breaches


# --------------------------------------------------------------------------
# Appointments
# --------------------------------------------------------------------------


class AppointmentError(Exception):
    """A user-facing reason a booking operation was refused."""


class SlotUnavailable(AppointmentError):
    """The requested time is taken or outside working hours."""


def _parse_hhmm(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def validate_working_hours(working_hours: dict) -> None:
    for key, windows in working_hours.items():
        if key not in WEEKDAYS:
            raise ValueError(f"unknown weekday {key!r}")
        for window in windows:
            if len(window) != 2:
                raise ValueError("each window must be [start, end]")
            start, end = _parse_hhmm(window[0]), _parse_hhmm(window[1])
            if start >= end:
                raise ValueError(f"window {window} must start before it ends")


def working_windows(working_hours: dict, day: date, tz: ZoneInfo) -> list[tuple[datetime, datetime]]:
    out = []
    for window in working_hours.get(WEEKDAYS[day.weekday()], []):
        start = datetime.combine(day, _parse_hhmm(window[0]), tzinfo=tz)
        end = datetime.combine(day, _parse_hhmm(window[1]), tzinfo=tz)
        out.append((start, end))
    return sorted(out)


def overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    return a_start < b_end and a_end > b_start


def generate_slots(
    day: date,
    working_hours: dict,
    tz_name: str,
    duration_minutes: int,
    busy: Iterable[tuple[datetime, datetime]],
    *,
    step_minutes: int = 15,
    earliest: datetime | None = None,
) -> list[tuple[datetime, datetime]]:
    """Free [start, end) slots (UTC) on `day` in the resource's timezone."""
    tz = _tz(tz_name)
    busy = [(_aware(s), _aware(e)) for s, e in busy]
    duration = timedelta(minutes=duration_minutes)
    step = timedelta(minutes=max(step_minutes, 1))
    slots = []
    for window_start, window_end in working_windows(working_hours, day, tz):
        cur = window_start
        while cur + duration <= window_end:
            end = cur + duration
            if (earliest is None or cur >= _aware(earliest)) and not any(
                overlaps(cur, end, b_start, b_end) for b_start, b_end in busy
            ):
                slots.append((cur.astimezone(UTC), end.astimezone(UTC)))
            cur += step
    return slots


def fits_working_hours(
    working_hours: dict, tz_name: str, start: datetime, end: datetime
) -> bool:
    tz = _tz(tz_name)
    local_start = _aware(start).astimezone(tz)
    if local_start.date() != _aware(end).astimezone(tz).date():
        return False
    return any(
        ws <= local_start and _aware(end) <= we
        for ws, we in working_windows(working_hours, local_start.date(), tz)
    )


async def _conflicts(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    resource_id: uuid.UUID,
    start: datetime,
    end: datetime,
    exclude_id: uuid.UUID | None = None,
) -> list[Appointment]:
    query = select(Appointment).where(
        Appointment.tenant_id == tenant_id,
        Appointment.resource_id == resource_id,
        Appointment.status.in_(ACTIVE_APPOINTMENT_STATUSES),
        Appointment.start_at < end,
        Appointment.end_at > start,
    )
    if exclude_id is not None:
        query = query.where(Appointment.id != exclude_id)
    return list((await db.execute(query)).scalars().all())


async def _load_service(db: AsyncSession, tenant_id: uuid.UUID, service_id: uuid.UUID) -> BookableService:
    service = await db.get(BookableService, service_id)
    if service is None or service.tenant_id != tenant_id:
        raise AppointmentError("service not found")
    if not service.is_active:
        raise AppointmentError("service is not currently bookable")
    return service


async def _active_resources(
    db: AsyncSession, tenant_id: uuid.UUID, resource_id: uuid.UUID | None
) -> list[AppointmentResource]:
    query = select(AppointmentResource).where(
        AppointmentResource.tenant_id == tenant_id, AppointmentResource.is_active.is_(True)
    )
    if resource_id is not None:
        query = query.where(AppointmentResource.id == resource_id)
    resources = list((await db.execute(query.order_by(AppointmentResource.name))).scalars().all())
    if resource_id is not None and not resources:
        raise AppointmentError("resource not found or inactive")
    return resources


async def free_slots(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    day: date,
    service_id: uuid.UUID,
    resource_id: uuid.UUID | None = None,
    *,
    now: datetime | None = None,
    step_minutes: int = 15,
) -> list[dict]:
    service = await _load_service(db, tenant_id, service_id)
    now = _aware(now or datetime.now(UTC))
    out = []
    # Opt-in: busy times from the tenant's connected Google/Outlook calendar
    # (best effort; empty when not connected or the provider is unreachable).
    window_start, window_end = calendar_sync.day_window(day)
    external_busy = await calendar_sync.external_busy(db, tenant_id, window_start, window_end)
    for resource in await _active_resources(db, tenant_id, resource_id):
        tz = _tz(resource.timezone)
        day_start = datetime.combine(day, time(0), tzinfo=tz)
        busy_rows = await db.execute(
            select(Appointment.start_at, Appointment.end_at).where(
                Appointment.tenant_id == tenant_id,
                Appointment.resource_id == resource.id,
                Appointment.status.in_(ACTIVE_APPOINTMENT_STATUSES),
                Appointment.start_at < day_start + timedelta(days=1),
                Appointment.end_at > day_start,
            )
        )
        slots = generate_slots(
            day,
            resource.working_hours,
            resource.timezone,
            service.duration_minutes,
            [*busy_rows.all(), *external_busy],
            step_minutes=step_minutes,
            earliest=now,
        )
        for start, end in slots:
            out.append(
                {
                    "resource_id": resource.id,
                    "resource_name": resource.name,
                    "start": start,
                    "end": end,
                }
            )
    out.sort(key=lambda s: (s["start"], s["resource_name"]))
    return out


async def _place(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    service: BookableService,
    candidates: list[AppointmentResource],
    start: datetime,
    exclude_id: uuid.UUID | None,
) -> AppointmentResource:
    end = start + timedelta(minutes=service.duration_minutes)
    saw_working = False
    for resource in candidates:
        # Row lock serialises concurrent bookings on the same resource so the
        # conflict check below cannot race.
        await db.execute(
            select(AppointmentResource.id)
            .where(AppointmentResource.id == resource.id)
            .with_for_update()
        )
        if not fits_working_hours(resource.working_hours, resource.timezone, start, end):
            continue
        saw_working = True
        if not await _conflicts(db, tenant_id, resource.id, start, end, exclude_id):
            return resource
    raise SlotUnavailable(
        "that time slot is already taken" if saw_working else "that time is outside working hours"
    )


async def book_appointment(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    customer_id: uuid.UUID,
    service_id: uuid.UUID,
    start_at: datetime,
    resource_id: uuid.UUID | None = None,
    source: str = "manual",
    notes: str | None = None,
    now: datetime | None = None,
) -> Appointment:
    start_at = _aware(start_at).astimezone(UTC)
    if start_at <= _aware(now or datetime.now(UTC)):
        raise AppointmentError("cannot book a time in the past")
    customer = await db.get(Customer, customer_id)
    if customer is None or customer.tenant_id != tenant_id:
        raise AppointmentError("customer not found")
    service = await _load_service(db, tenant_id, service_id)
    resource = await _place(
        db, tenant_id, service, await _active_resources(db, tenant_id, resource_id), start_at, None
    )
    appointment = Appointment(
        tenant_id=tenant_id,
        customer_id=customer_id,
        service_id=service.id,
        resource_id=resource.id,
        start_at=start_at,
        end_at=start_at + timedelta(minutes=service.duration_minutes),
        status="booked",
        source=source,
        notes=notes,
    )
    db.add(appointment)
    await db.flush()
    await calendar_sync.sync_appointment(db, appointment, "create")  # best effort, never raises
    return appointment


async def get_appointment(db: AsyncSession, tenant_id: uuid.UUID, appointment_id: uuid.UUID) -> Appointment:
    appointment = await db.get(Appointment, appointment_id)
    if appointment is None or appointment.tenant_id != tenant_id:
        raise AppointmentError("appointment not found")
    return appointment


async def reschedule_appointment(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    appointment_id: uuid.UUID,
    *,
    start_at: datetime,
    resource_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> Appointment:
    appointment = await get_appointment(db, tenant_id, appointment_id)
    if appointment.status not in ACTIVE_APPOINTMENT_STATUSES:
        raise AppointmentError(f"a {appointment.status} appointment cannot be rescheduled")
    start_at = _aware(start_at).astimezone(UTC)
    if start_at <= _aware(now or datetime.now(UTC)):
        raise AppointmentError("cannot move an appointment into the past")
    service = await db.get(BookableService, appointment.service_id)
    candidates = await _active_resources(db, tenant_id, resource_id or appointment.resource_id)
    resource = await _place(db, tenant_id, service, candidates, start_at, appointment.id)
    appointment.resource_id = resource.id
    appointment.start_at = start_at
    appointment.end_at = start_at + timedelta(minutes=service.duration_minutes)
    appointment.status = "booked"
    appointment.reminder_sent_at = None
    appointment.reminder_attempts = 0
    appointment.reminder_error = None
    await db.flush()
    await calendar_sync.sync_appointment(db, appointment, "update")  # best effort, never raises
    return appointment


async def cancel_appointment(
    db: AsyncSession, tenant_id: uuid.UUID, appointment_id: uuid.UUID, reason: str | None = None
) -> Appointment:
    appointment = await get_appointment(db, tenant_id, appointment_id)
    if appointment.status not in ACTIVE_APPOINTMENT_STATUSES:
        raise AppointmentError(f"a {appointment.status} appointment cannot be cancelled")
    appointment.status = "cancelled"
    appointment.cancel_reason = reason
    await db.flush()
    await calendar_sync.sync_appointment(db, appointment, "cancel")  # best effort, never raises
    return appointment


async def set_appointment_status(
    db: AsyncSession, tenant_id: uuid.UUID, appointment_id: uuid.UUID, status: str
) -> Appointment:
    appointment = await get_appointment(db, tenant_id, appointment_id)
    if appointment.status == "cancelled":
        raise AppointmentError("a cancelled appointment cannot change status")
    if status in ("completed", "no_show") and appointment.start_at > datetime.now(UTC):
        raise AppointmentError(f"cannot mark a future appointment {status}")
    appointment.status = status
    await db.flush()
    return appointment


def reminder_text(customer_name: str, service_name: str, start: datetime, tz_name: str) -> str:
    local = _aware(start).astimezone(_tz(tz_name))
    return (
        f"Hi {customer_name}, a reminder of your {service_name} appointment on "
        f"{local:%A %d %b} at {local:%H:%M} ({tz_name}). Reply to this message to reschedule."
    )


@dataclass
class ReminderResult:
    sent: int = 0
    failed: int = 0


async def send_due_reminders(
    db: AsyncSession, *, now: datetime | None = None, tenant_id: uuid.UUID | None = None
) -> ReminderResult:
    now = _aware(now or datetime.now(UTC))
    query = select(Appointment).where(
        Appointment.status.in_(ACTIVE_APPOINTMENT_STATUSES),
        Appointment.reminder_sent_at.is_(None),
        Appointment.reminder_attempts < MAX_REMINDER_ATTEMPTS,
        Appointment.start_at > now,
        Appointment.start_at <= now + REMINDER_LEAD,
    )
    if tenant_id is not None:
        query = query.where(Appointment.tenant_id == tenant_id)
    result = ReminderResult()
    for appointment in (await db.execute(query.with_for_update(skip_locked=True))).scalars().all():
        customer = await db.get(Customer, appointment.customer_id)
        service = await db.get(BookableService, appointment.service_id)
        resource = await db.get(AppointmentResource, appointment.resource_id)
        appointment.reminder_attempts += 1
        try:
            if customer is None or not customer.phone:
                raise AppointmentError("customer has no phone number")
            account = await _whatsapp_account(db, appointment.tenant_id)
            # An approved reminder template reaches customers who haven't written in
            # 24h (plain text only works inside WhatsApp's 24-hour window).
            from app.services import whatsapp_templates as tpl

            template = await tpl.find_approved(db, appointment.tenant_id, purpose="booking_reminder")
            if template is not None:
                values = await tpl.context_values(db, appointment.tenant_id, customer, appointment=appointment)
                await tpl.send_template(
                    db, appointment.tenant_id, to=customer.phone, template=template, values=values, account=account
                )
            else:
                await send_text_message(
                    account,
                    to=_digits(customer.phone),
                    body=reminder_text(customer.name, service.name, appointment.start_at, resource.timezone),
                )
            appointment.reminder_sent_at = now
            appointment.reminder_error = None
            result.sent += 1
        except (AppointmentError, WhatsAppAPIError, plans.PlanLimitError) as exc:
            appointment.reminder_error = str(exc)[:500]
            logger.warning("appointment %s reminder failed: %s", appointment.id, exc)
            result.failed += 1
        await db.commit()
    return result


async def create_no_show_followups(
    db: AsyncSession, *, now: datetime | None = None, tenant_id: uuid.UUID | None = None
) -> int:
    now = _aware(now or datetime.now(UTC))
    query = select(Appointment).where(
        Appointment.status == "no_show", Appointment.no_show_followup_at.is_(None)
    )
    if tenant_id is not None:
        query = query.where(Appointment.tenant_id == tenant_id)
    created = 0
    for appointment in (await db.execute(query.with_for_update(skip_locked=True))).scalars().all():
        customer = await db.get(Customer, appointment.customer_id)
        service = await db.get(BookableService, appointment.service_id)
        if customer is not None and customer.phone:
            db.add(
                FollowUp(
                    tenant_id=appointment.tenant_id,
                    customer_id=customer.id,
                    channel="whatsapp",
                    due_at=now,
                    created_by="workflow",
                    reference_type="appointment",
                    reference_id=appointment.id,
                    payload={
                        "message": f"Hi {customer.name}, we missed you at your {service.name} "
                        "appointment. Would you like to book another time? Just reply here."
                    },
                )
            )
            created += 1
        appointment.no_show_followup_at = now
        await db.commit()
    return created


# --------------------------------------------------------------------------
# Follow-ups
# --------------------------------------------------------------------------


class FollowUpError(Exception):
    def __init__(self, message: str, *, permanent: bool = False):
        super().__init__(message)
        self.permanent = permanent


def _digits(phone: str) -> str:
    return "".join(ch for ch in phone if ch.isdigit())


async def _whatsapp_account(db: AsyncSession, tenant_id: uuid.UUID) -> WhatsAppAccount:
    account = (
        (await db.execute(select(WhatsAppAccount).where(WhatsAppAccount.tenant_id == tenant_id)))
        .scalars()
        .first()
    )
    if account is None:
        raise AppointmentError("no WhatsApp number connected for this business")
    return account


async def select_due_follow_ups(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    limit: int = 50,
    tenant_id: uuid.UUID | None = None,
    lock: bool = True,
) -> list[FollowUp]:
    """Pending follow-ups whose due time has arrived, oldest first."""
    query = select(FollowUp).where(
        FollowUp.status == "pending", FollowUp.due_at <= _aware(now or datetime.now(UTC))
    )
    if tenant_id is not None:
        query = query.where(FollowUp.tenant_id == tenant_id)
    query = query.order_by(FollowUp.due_at).limit(limit)
    if lock:
        query = query.with_for_update(skip_locked=True)
    return list((await db.execute(query)).scalars().all())


async def _send_sms(db: AsyncSession, tenant_id: uuid.UUID, to: str, body: str) -> None:
    from app.integrations.channel_senders import ChannelSendError, send_sms_message

    number = (
        (await db.execute(select(PhoneNumber).where(PhoneNumber.tenant_id == tenant_id)))
        .scalars()
        .first()
    )
    if number is None:
        raise FollowUpError("no phone number to send SMS from", permanent=True)
    try:
        await send_sms_message(db, tenant_id, to=to, from_=number.e164_number, body=body)
    except ChannelSendError as exc:
        raise FollowUpError(str(exc)) from exc


async def _dispatch_follow_up(db: AsyncSession, follow_up: FollowUp, customer: Customer) -> None:
    payload = follow_up.payload or {}
    message = payload.get("message") or ""
    channel = follow_up.channel
    if channel in ("call", "whatsapp", "sms") and not customer.phone:
        raise FollowUpError("customer has no phone number", permanent=True)

    if channel == "call":
        # Lazy: outbound_call -> workflows -> tools.registry -> handlers imports
        # this module (apply_sla_safely), so a top-level import would be circular.
        from app.services.outbound_call import OutboundCallError, place_ai_outbound_call

        try:
            from app.services import callbacks

            await place_ai_outbound_call(
                db,
                follow_up.tenant_id,
                to_number=payload.get("to_number") or customer.phone,
                ai_agent_id=payload.get("ai_agent_id"),
                from_number=payload.get("from_number"),
                mission=callbacks.mission_for(payload) if payload.get("callback_of") else None,
            )
        except OutboundCallError as exc:
            raise FollowUpError(str(exc)) from exc
    elif channel == "whatsapp":
        try:
            account = await _whatsapp_account(db, follow_up.tenant_id)
        except AppointmentError as exc:
            raise FollowUpError(str(exc), permanent=True) from exc
        try:
            if payload.get("template_name"):
                from app.integrations.whatsapp import send_template_message
                from app.services import whatsapp_templates as tpl

                template = await tpl.find_approved(db, follow_up.tenant_id, name=payload["template_name"])
                if template is not None:  # one of the business's own templates: fill its blanks
                    values = await tpl.context_values(
                        db, follow_up.tenant_id, customer, extra=payload.get("values") or {}
                    )
                    await tpl.send_template(
                        db, follow_up.tenant_id, to=customer.phone, template=template, values=values, account=account
                    )
                else:
                    await send_template_message(
                        account,
                        to=_digits(customer.phone),
                        template_name=payload["template_name"],
                        language_code=payload.get("language_code", "en_US"),
                    )
            elif message:
                await send_text_message(account, to=_digits(customer.phone), body=message)
            else:
                raise FollowUpError("payload needs a message or template_name", permanent=True)
        except plans.PlanLimitError as exc:
            raise FollowUpError(exc.message) from exc
        except WhatsAppAPIError as exc:
            raise FollowUpError(str(exc)) from exc
    elif channel == "sms":
        if not message:
            raise FollowUpError("payload needs a message", permanent=True)
        await _send_sms(db, follow_up.tenant_id, customer.phone, message)
    elif channel == "email":
        if not get_settings().smtp_host:
            raise FollowUpError("email is not configured (SMTP host unset)", permanent=True)
        if not customer.email:
            raise FollowUpError("customer has no email address", permanent=True)
        if not message:
            raise FollowUpError("payload needs a message", permanent=True)
        reserved = None
        try:
            reserved = await plans.reserve_email(db, follow_up.tenant_id)
            await send_email(
                to_email=customer.email,
                subject=payload.get("subject") or "Following up",
                html_body=html.escape(message).replace("\n", "<br>"),
            )
        except Exception as exc:
            if reserved is not None:
                await db.delete(reserved)
                await db.flush()
            raise FollowUpError(f"email failed: {exc}") from exc
    else:
        raise FollowUpError(f"unsupported channel {channel!r}", permanent=True)


async def execute_follow_up(db: AsyncSession, follow_up: FollowUp, *, now: datetime | None = None) -> None:
    now = _aware(now or datetime.now(UTC))
    follow_up.attempts += 1
    customer = await db.get(Customer, follow_up.customer_id)
    try:
        if customer is None or customer.tenant_id != follow_up.tenant_id:
            raise FollowUpError("customer no longer exists", permanent=True)
        if follow_up.channel == "auto":
            from app.services import outreach  # the AI picks the channel, writes the message and falls back

            await outreach.run_step(db, follow_up, customer, now)
            return
        await _dispatch_follow_up(db, follow_up, customer)
    except FollowUpError as exc:
        follow_up.last_error = str(exc)[:1000]
        if exc.permanent or follow_up.attempts >= MAX_FOLLOW_UP_ATTEMPTS:
            follow_up.status = "failed"
            follow_up.executed_at = now
        else:
            follow_up.due_at = now + FOLLOW_UP_RETRY_DELAY
        return
    follow_up.status = "done"
    follow_up.last_error = None
    follow_up.executed_at = now


async def run_due_follow_ups(
    db: AsyncSession, *, now: datetime | None = None, limit: int = 50
) -> dict[str, int]:
    now = _aware(now or datetime.now(UTC))
    counts = {"done": 0, "failed": 0, "retry": 0}
    for follow_up in await select_due_follow_ups(db, now=now, limit=limit):
        await execute_follow_up(db, follow_up, now=now)
        counts[{"done": "done", "failed": "failed"}.get(follow_up.status, "retry")] += 1
        await db.commit()
    return counts


# --------------------------------------------------------------------------
# Collections
# --------------------------------------------------------------------------


def promise_reminder_due(due_date: date, now: datetime) -> datetime:
    """09:00 UTC on the promised date, or immediately if that has passed."""
    target = datetime.combine(due_date, time(9), tzinfo=UTC)
    return max(target, _aware(now))


def reminder_message(customer_name: str, amount: Decimal, currency: str, due_date: date) -> str:
    return (
        f"Hi {customer_name}, a friendly reminder that your payment of {currency} {amount:,.2f} "
        f"promised for {due_date:%d %b %Y} is due. Please reply if you need a payment link."
    )


async def create_payment_promise(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    customer_id: uuid.UUID,
    amount: Decimal,
    due_date: date,
    currency: str = "INR",
    notes: str | None = None,
    created_by: str = "human",
    call_id: uuid.UUID | None = None,
    reminder_channel: str = "whatsapp",
    now: datetime | None = None,
) -> tuple[PaymentPromise, FollowUp | None]:
    now = _aware(now or datetime.now(UTC))
    customer = await db.get(Customer, customer_id)
    if customer is None or customer.tenant_id != tenant_id:
        raise ValueError("customer not found")
    promise = PaymentPromise(
        tenant_id=tenant_id,
        customer_id=customer_id,
        amount=amount,
        currency=currency.upper(),
        due_date=due_date,
        promised_on=now.date(),
        notes=notes,
        created_by=created_by,
        call_id=call_id,
    )
    db.add(promise)
    await db.flush()
    follow_up = await schedule_promise_reminder(
        db, promise, customer, channel=reminder_channel, now=now
    )
    return promise, follow_up


async def schedule_promise_reminder(
    db: AsyncSession,
    promise: PaymentPromise,
    customer: Customer,
    *,
    channel: str = "whatsapp",
    now: datetime | None = None,
    immediate: bool = False,
) -> FollowUp | None:
    if not customer.phone and channel != "email":
        return None
    now = _aware(now or datetime.now(UTC))
    follow_up = FollowUp(
        tenant_id=promise.tenant_id,
        customer_id=customer.id,
        channel=channel,
        due_at=now if immediate else promise_reminder_due(promise.due_date, now),
        created_by="workflow",
        reference_type="payment_promise",
        reference_id=promise.id,
        payload={
            "message": reminder_message(customer.name, promise.amount, promise.currency, promise.due_date),
            "subject": "Payment reminder",
        },
    )
    db.add(follow_up)
    await db.flush()
    return follow_up


async def cancel_promise_reminders(db: AsyncSession, promise_id: uuid.UUID) -> None:
    await db.execute(
        update(FollowUp)
        .where(
            FollowUp.reference_type == "payment_promise",
            FollowUp.reference_id == promise_id,
            FollowUp.status == "pending",
        )
        .values(status="cancelled")
    )


def is_overdue(promise: PaymentPromise, today: date) -> bool:
    return promise.status == "pending" and promise.due_date < today
