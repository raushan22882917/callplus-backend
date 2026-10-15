import uuid
from collections import defaultdict
from datetime import UTC, date, datetime
from typing import Literal

from app.core.deps import RequestContext, get_db, require_permission
from app.core.exceptions import AppError, ConflictError, NotFoundError
from app.core.permissions import Permissions
from app.core.repository import TenantScopedRepository
from app.schemas import operations as s
from app.services import operations as svc
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import Customer, Ticket
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

router = APIRouter(prefix="/operations", tags=["operations"])

SLA_READ = Permissions.TICKETS_READ
SLA_MANAGE = Permissions.TICKETS_MANAGE
CUSTOMER_READ = Permissions.CUSTOMERS_READ
CUSTOMER_MANAGE = Permissions.CUSTOMERS_MANAGE
PAYMENTS_READ = Permissions.PAYMENTS_READ
PAYMENTS_MANAGE = Permissions.PAYMENTS_MANAGE


class BadRequest(AppError):
    status_code = 400


def _appointment_http_error(exc: svc.AppointmentError) -> AppError:
    if isinstance(exc, svc.SlotUnavailable):
        return ConflictError(str(exc))
    if "not found" in str(exc):
        return NotFoundError(str(exc))
    return BadRequest(str(exc))


def _repo(model):
    return type(f"{model.__name__}Repo", (TenantScopedRepository,), {"model": model})


async def _commit_unique(db: AsyncSession, message: str) -> None:
    from sqlalchemy.exc import IntegrityError

    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise ConflictError(message) from exc


async def _create_unique(db: AsyncSession, model, tenant_id: uuid.UUID, message: str, data: dict):
    from sqlalchemy.exc import IntegrityError

    try:
        obj = await _repo(model)(db, tenant_id).create(**data)
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise ConflictError(message) from exc
    await db.refresh(obj)
    return obj


# ============================ categories ====================================


@router.get("/categories", response_model=list[s.CategoryRead])
async def list_categories(
    ctx: RequestContext = Depends(require_permission(SLA_READ)),
    db: AsyncSession = Depends(get_db),
) -> list[TicketCategory]:
    rows = await db.execute(
        select(TicketCategory)
        .where(TicketCategory.tenant_id == ctx.tenant_id)
        .order_by(TicketCategory.department, TicketCategory.name)
    )
    return list(rows.scalars().all())


@router.post("/categories", response_model=s.CategoryRead, status_code=201)
async def create_category(
    payload: s.CategoryCreate,
    ctx: RequestContext = Depends(require_permission(Permissions.SETTINGS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> TicketCategory:
    return await _create_unique(
        db, TicketCategory, ctx.tenant_id, "A category with that name already exists",
        payload.model_dump(),
    )


@router.patch("/categories/{category_id}", response_model=s.CategoryRead)
async def update_category(
    category_id: uuid.UUID,
    payload: s.CategoryUpdate,
    ctx: RequestContext = Depends(require_permission(Permissions.SETTINGS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> TicketCategory:
    category = await _repo(TicketCategory)(db, ctx.tenant_id).get(category_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(category, field, value)
    await _commit_unique(db, "A category with that name already exists")
    await db.refresh(category)
    return category


@router.delete("/categories/{category_id}", status_code=204)
async def delete_category(
    category_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(Permissions.SETTINGS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> None:
    await _repo(TicketCategory)(db, ctx.tenant_id).delete(category_id)
    await db.commit()


# ================================ SLA =======================================


def _validate_policy(first: int, resolution: int, start: int, end: int, tz: str) -> None:
    if resolution < first:
        raise BadRequest("resolution_minutes must be at least first_response_minutes")
    if start >= end:
        raise BadRequest("business_start_hour must be before business_end_hour")
    try:
        svc._tz(tz)
    except ValueError as exc:
        raise BadRequest(str(exc)) from exc


@router.get("/sla/policies", response_model=list[s.SLAPolicyRead])
async def list_sla_policies(
    ctx: RequestContext = Depends(require_permission(SLA_READ)),
    db: AsyncSession = Depends(get_db),
) -> list[SLAPolicy]:
    rows = await db.execute(select(SLAPolicy).where(SLAPolicy.tenant_id == ctx.tenant_id))
    order = {"urgent": 0, "high": 1, "medium": 2, "low": 3}
    return sorted(rows.scalars().all(), key=lambda p: order.get(p.priority, 9))


@router.post("/sla/policies", response_model=s.SLAPolicyRead, status_code=201)
async def create_sla_policy(
    payload: s.SLAPolicyCreate,
    ctx: RequestContext = Depends(require_permission(Permissions.SETTINGS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> SLAPolicy:
    _validate_policy(
        payload.first_response_minutes,
        payload.resolution_minutes,
        payload.business_start_hour,
        payload.business_end_hour,
        payload.timezone,
    )
    return await _create_unique(
        db, SLAPolicy, ctx.tenant_id,
        f"A policy for priority '{payload.priority}' already exists", payload.model_dump(),
    )


@router.patch("/sla/policies/{policy_id}", response_model=s.SLAPolicyRead)
async def update_sla_policy(
    policy_id: uuid.UUID,
    payload: s.SLAPolicyUpdate,
    ctx: RequestContext = Depends(require_permission(Permissions.SETTINGS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> SLAPolicy:
    policy = await _repo(SLAPolicy)(db, ctx.tenant_id).get(policy_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(policy, field, value)
    _validate_policy(
        policy.first_response_minutes,
        policy.resolution_minutes,
        policy.business_start_hour,
        policy.business_end_hour,
        policy.timezone,
    )
    await db.commit()
    await db.refresh(policy)
    return policy


@router.delete("/sla/policies/{policy_id}", status_code=204)
async def delete_sla_policy(
    policy_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(Permissions.SETTINGS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> None:
    await _repo(SLAPolicy)(db, ctx.tenant_id).delete(policy_id)
    await db.commit()


def _sla_row(sla: TicketSLA, ticket: Ticket, now: datetime) -> s.TicketSLARead:
    first = svc.sla_clock(sla.started_at, sla.first_response_due_at, sla.first_responded_at, now)
    resolution = svc.sla_clock(sla.started_at, sla.resolution_due_at, sla.resolved_at, now)
    return s.TicketSLARead(
        ticket_id=sla.ticket_id,
        subject=ticket.subject,
        ticket_status=ticket.status.value,
        priority=sla.priority,
        department=sla.department,
        category_id=sla.category_id,
        started_at=sla.started_at,
        first_response=s.SLAClock(due_at=sla.first_response_due_at, **first),
        resolution=s.SLAClock(due_at=sla.resolution_due_at, **resolution),
        breached=first["state"] == "breached" or resolution["state"] == "breached",
    )


async def _sla_rows(db: AsyncSession, tenant_id: uuid.UUID) -> list[tuple[TicketSLA, Ticket]]:
    rows = await db.execute(
        select(TicketSLA, Ticket)
        .join(Ticket, Ticket.id == TicketSLA.ticket_id)
        .where(TicketSLA.tenant_id == tenant_id)
        .order_by(TicketSLA.resolution_due_at)
    )
    return list(rows.all())


@router.get("/sla/tickets", response_model=list[s.TicketSLARead])
async def list_ticket_sla(
    breached: bool | None = None,
    open_only: bool = False,
    priority: s.Priority | None = None,
    department: str | None = None,
    limit: int = Query(default=100, le=500),
    ctx: RequestContext = Depends(require_permission(SLA_READ)),
    db: AsyncSession = Depends(get_db),
) -> list[s.TicketSLARead]:
    now = datetime.now(UTC)
    out = []
    for sla, ticket in await _sla_rows(db, ctx.tenant_id):
        if priority and sla.priority != priority:
            continue
        if department and sla.department != department:
            continue
        if open_only and sla.resolved_at is not None:
            continue
        row = _sla_row(sla, ticket, now)
        if breached is not None and row.breached != breached:
            continue
        out.append(row)
    return out[:limit]


@router.patch("/sla/tickets/{ticket_id}", response_model=s.TicketSLARead)
async def set_ticket_category(
    ticket_id: uuid.UUID,
    payload: s.TicketSLACategoryUpdate,
    ctx: RequestContext = Depends(require_permission(SLA_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> s.TicketSLARead:
    sla = (
        await db.execute(
            select(TicketSLA).where(
                TicketSLA.ticket_id == ticket_id, TicketSLA.tenant_id == ctx.tenant_id
            )
        )
    ).scalar_one_or_none()
    if sla is None:
        raise NotFoundError("No SLA is tracked for that ticket")
    if payload.category_id is None:
        sla.category_id, sla.department = None, None
    else:
        category = await _repo(TicketCategory)(db, ctx.tenant_id).get(payload.category_id)
        sla.category_id, sla.department = category.id, category.department
    await db.commit()
    ticket = await db.get(Ticket, ticket_id)
    return _sla_row(sla, ticket, datetime.now(UTC))


@router.get("/sla/summary", response_model=list[s.SLASummaryRow])
async def sla_summary(
    ctx: RequestContext = Depends(require_permission(SLA_READ)),
    db: AsyncSession = Depends(get_db),
) -> list[s.SLASummaryRow]:
    now = datetime.now(UTC)
    groups: dict[tuple[str, str], dict[str, int]] = defaultdict(
        lambda: {"total": 0, "open": 0, "breached": 0, "at_risk": 0, "met": 0}
    )
    for sla, ticket in await _sla_rows(db, ctx.tenant_id):
        row = _sla_row(sla, ticket, now)
        g = groups[(sla.department or "Unassigned", sla.priority)]
        g["total"] += 1
        if sla.resolved_at is None:
            g["open"] += 1
        if row.breached:
            g["breached"] += 1
        elif row.first_response.state == "at_risk" or row.resolution.state == "at_risk":
            g["at_risk"] += 1
        elif sla.resolved_at is not None:
            g["met"] += 1
    out = []
    for (department, priority), g in sorted(groups.items()):
        decided = g["breached"] + g["met"]
        out.append(
            s.SLASummaryRow(
                department=department,
                priority=priority,
                compliance_pct=round(100 * g["met"] / decided, 1) if decided else None,
                **g,
            )
        )
    return out


# ============================= appointments =================================


@router.get("/services", response_model=list[s.ServiceRead])
async def list_services(
    ctx: RequestContext = Depends(require_permission(CUSTOMER_READ)),
    db: AsyncSession = Depends(get_db),
) -> list[BookableService]:
    rows = await db.execute(
        select(BookableService)
        .where(BookableService.tenant_id == ctx.tenant_id)
        .order_by(BookableService.name)
    )
    return list(rows.scalars().all())


@router.post("/services", response_model=s.ServiceRead, status_code=201)
async def create_service(
    payload: s.ServiceCreate,
    ctx: RequestContext = Depends(require_permission(CUSTOMER_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> BookableService:
    return await _create_unique(
        db, BookableService, ctx.tenant_id, "A service with that name already exists",
        payload.model_dump(),
    )


@router.patch("/services/{service_id}", response_model=s.ServiceRead)
async def update_service(
    service_id: uuid.UUID,
    payload: s.ServiceUpdate,
    ctx: RequestContext = Depends(require_permission(CUSTOMER_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> BookableService:
    service = await _repo(BookableService)(db, ctx.tenant_id).get(service_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(service, field, value)
    await _commit_unique(db, "A service with that name already exists")
    await db.refresh(service)
    return service


@router.delete("/services/{service_id}", status_code=204)
async def delete_service(
    service_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(CUSTOMER_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> None:
    await _repo(BookableService)(db, ctx.tenant_id).delete(service_id)
    await _commit_unique(db, "Service has appointments; deactivate it instead")


@router.get("/resources", response_model=list[s.ResourceRead])
async def list_resources(
    ctx: RequestContext = Depends(require_permission(CUSTOMER_READ)),
    db: AsyncSession = Depends(get_db),
) -> list[AppointmentResource]:
    rows = await db.execute(
        select(AppointmentResource)
        .where(AppointmentResource.tenant_id == ctx.tenant_id)
        .order_by(AppointmentResource.name)
    )
    return list(rows.scalars().all())


@router.post("/resources", response_model=s.ResourceRead, status_code=201)
async def create_resource(
    payload: s.ResourceCreate,
    ctx: RequestContext = Depends(require_permission(CUSTOMER_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> AppointmentResource:
    resource = await _repo(AppointmentResource)(db, ctx.tenant_id).create(**payload.model_dump())
    await db.commit()
    await db.refresh(resource)
    return resource


@router.patch("/resources/{resource_id}", response_model=s.ResourceRead)
async def update_resource(
    resource_id: uuid.UUID,
    payload: s.ResourceUpdate,
    ctx: RequestContext = Depends(require_permission(CUSTOMER_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> AppointmentResource:
    resource = await _repo(AppointmentResource)(db, ctx.tenant_id).get(resource_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(resource, field, value)
    await db.commit()
    await db.refresh(resource)
    return resource


@router.delete("/resources/{resource_id}", status_code=204)
async def delete_resource(
    resource_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(CUSTOMER_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> None:
    await _repo(AppointmentResource)(db, ctx.tenant_id).delete(resource_id)
    await _commit_unique(db, "Resource has appointments; deactivate it instead")


@router.get("/availability", response_model=list[s.SlotRead])
async def get_availability(
    day: date = Query(alias="date"),
    service_id: uuid.UUID = Query(),
    resource_id: uuid.UUID | None = None,
    ctx: RequestContext = Depends(require_permission(CUSTOMER_READ)),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    try:
        return await svc.free_slots(db, ctx.tenant_id, day, service_id, resource_id)
    except svc.AppointmentError as exc:
        raise _appointment_http_error(exc) from exc


async def _enrich_appointments(
    db: AsyncSession, tenant_id: uuid.UUID, appointments: list[Appointment]
) -> list[s.AppointmentRead]:
    if not appointments:
        return []
    customers = {
        c.id: c.name
        for c in (
            await db.execute(
                select(Customer).where(
                    Customer.tenant_id == tenant_id,
                    Customer.id.in_({a.customer_id for a in appointments}),
                )
            )
        ).scalars()
    }
    services = {
        x.id: x.name
        for x in (
            await db.execute(select(BookableService).where(BookableService.tenant_id == tenant_id))
        ).scalars()
    }
    resources = {
        x.id: x.name
        for x in (
            await db.execute(
                select(AppointmentResource).where(AppointmentResource.tenant_id == tenant_id)
            )
        ).scalars()
    }
    out = []
    for a in appointments:
        row = s.AppointmentRead.model_validate(a)
        row.customer_name = customers.get(a.customer_id)
        row.service_name = services.get(a.service_id)
        row.resource_name = resources.get(a.resource_id)
        out.append(row)
    return out


@router.get("/appointments", response_model=list[s.AppointmentRead])
async def list_appointments(
    start: datetime | None = Query(default=None, alias="from"),
    end: datetime | None = Query(default=None, alias="to"),
    resource_id: uuid.UUID | None = None,
    customer_id: uuid.UUID | None = None,
    status_filter: s.AppointmentStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=500, le=1000),
    ctx: RequestContext = Depends(require_permission(CUSTOMER_READ)),
    db: AsyncSession = Depends(get_db),
) -> list[s.AppointmentRead]:
    query = select(Appointment).where(Appointment.tenant_id == ctx.tenant_id)
    if start:
        query = query.where(Appointment.end_at > svc._aware(start))
    if end:
        query = query.where(Appointment.start_at < svc._aware(end))
    if resource_id:
        query = query.where(Appointment.resource_id == resource_id)
    if customer_id:
        query = query.where(Appointment.customer_id == customer_id)
    if status_filter:
        query = query.where(Appointment.status == status_filter)
    rows = await db.execute(query.order_by(Appointment.start_at).limit(limit))
    return await _enrich_appointments(db, ctx.tenant_id, list(rows.scalars().all()))


@router.post("/appointments", response_model=s.AppointmentRead, status_code=201)
async def create_appointment(
    payload: s.AppointmentBook,
    ctx: RequestContext = Depends(require_permission(CUSTOMER_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> s.AppointmentRead:
    try:
        appointment = await svc.book_appointment(
            db,
            ctx.tenant_id,
            customer_id=payload.customer_id,
            service_id=payload.service_id,
            start_at=payload.start_at,
            resource_id=payload.resource_id,
            source=payload.source,
            notes=payload.notes,
        )
    except svc.AppointmentError as exc:
        await db.rollback()
        raise _appointment_http_error(exc) from exc
    await db.commit()
    return (await _enrich_appointments(db, ctx.tenant_id, [appointment]))[0]


@router.get("/appointments/{appointment_id}", response_model=s.AppointmentRead)
async def get_appointment(
    appointment_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(CUSTOMER_READ)),
    db: AsyncSession = Depends(get_db),
) -> s.AppointmentRead:
    try:
        appointment = await svc.get_appointment(db, ctx.tenant_id, appointment_id)
    except svc.AppointmentError as exc:
        raise _appointment_http_error(exc) from exc
    return (await _enrich_appointments(db, ctx.tenant_id, [appointment]))[0]


async def _mutate_appointment(db, ctx, action) -> s.AppointmentRead:
    try:
        appointment = await action()
    except svc.AppointmentError as exc:
        await db.rollback()
        raise _appointment_http_error(exc) from exc
    await db.commit()
    return (await _enrich_appointments(db, ctx.tenant_id, [appointment]))[0]


@router.post("/appointments/{appointment_id}/reschedule", response_model=s.AppointmentRead)
async def reschedule_appointment(
    appointment_id: uuid.UUID,
    payload: s.AppointmentReschedule,
    ctx: RequestContext = Depends(require_permission(CUSTOMER_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> s.AppointmentRead:
    return await _mutate_appointment(
        db,
        ctx,
        lambda: svc.reschedule_appointment(
            db, ctx.tenant_id, appointment_id, start_at=payload.start_at,
            resource_id=payload.resource_id,
        ),
    )


@router.post("/appointments/{appointment_id}/cancel", response_model=s.AppointmentRead)
async def cancel_appointment(
    appointment_id: uuid.UUID,
    payload: s.AppointmentCancel,
    ctx: RequestContext = Depends(require_permission(CUSTOMER_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> s.AppointmentRead:
    return await _mutate_appointment(
        db, ctx, lambda: svc.cancel_appointment(db, ctx.tenant_id, appointment_id, payload.reason)
    )


@router.post("/appointments/{appointment_id}/status", response_model=s.AppointmentRead)
async def update_appointment_status(
    appointment_id: uuid.UUID,
    payload: s.AppointmentStatusUpdate,
    ctx: RequestContext = Depends(require_permission(CUSTOMER_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> s.AppointmentRead:
    return await _mutate_appointment(
        db, ctx, lambda: svc.set_appointment_status(db, ctx.tenant_id, appointment_id, payload.status)
    )


# ============================== follow-ups ==================================


async def _enrich_follow_ups(
    db: AsyncSession, tenant_id: uuid.UUID, follow_ups: list[FollowUp]
) -> list[s.FollowUpRead]:
    if not follow_ups:
        return []
    names = {
        c.id: c.name
        for c in (
            await db.execute(
                select(Customer).where(
                    Customer.tenant_id == tenant_id,
                    Customer.id.in_({f.customer_id for f in follow_ups}),
                )
            )
        ).scalars()
    }
    out = []
    for f in follow_ups:
        row = s.FollowUpRead.model_validate(f)
        row.customer_name = names.get(f.customer_id)
        out.append(row)
    return out


@router.get("/follow-ups", response_model=list[s.FollowUpRead])
async def list_follow_ups(
    status_filter: s.FollowUpStatus | None = Query(default=None, alias="status"),
    channel: s.FollowUpChannel | None = None,
    customer_id: uuid.UUID | None = None,
    limit: int = Query(default=200, le=500),
    ctx: RequestContext = Depends(require_permission(CUSTOMER_READ)),
    db: AsyncSession = Depends(get_db),
) -> list[s.FollowUpRead]:
    query = select(FollowUp).where(FollowUp.tenant_id == ctx.tenant_id)
    if status_filter:
        query = query.where(FollowUp.status == status_filter)
    if channel:
        query = query.where(FollowUp.channel == channel)
    if customer_id:
        query = query.where(FollowUp.customer_id == customer_id)
    rows = await db.execute(query.order_by(FollowUp.due_at.desc()).limit(limit))
    return await _enrich_follow_ups(db, ctx.tenant_id, list(rows.scalars().all()))


@router.post("/follow-ups", response_model=s.FollowUpRead, status_code=201)
async def create_follow_up(
    payload: s.FollowUpCreate,
    ctx: RequestContext = Depends(require_permission(CUSTOMER_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> s.FollowUpRead:
    customer = await db.get(Customer, payload.customer_id)
    if customer is None or customer.tenant_id != ctx.tenant_id:
        raise NotFoundError("Customer not found")
    if payload.channel != "call" and not (payload.message or payload.template_name):
        raise BadRequest("A message (or WhatsApp template_name) is required for this channel")
    body = {
        k: v
        for k, v in {
            "message": payload.message,
            "subject": payload.subject,
            "template_name": payload.template_name,
            "ai_agent_id": str(payload.ai_agent_id) if payload.ai_agent_id else None,
        }.items()
        if v
    }
    follow_up = FollowUp(
        tenant_id=ctx.tenant_id,
        customer_id=payload.customer_id,
        channel=payload.channel,
        due_at=svc._aware(payload.due_at),
        payload=body,
        created_by="human",
    )
    db.add(follow_up)
    await db.commit()
    await db.refresh(follow_up)
    return (await _enrich_follow_ups(db, ctx.tenant_id, [follow_up]))[0]


@router.patch("/follow-ups/{follow_up_id}", response_model=s.FollowUpRead)
async def update_follow_up(
    follow_up_id: uuid.UUID,
    payload: s.FollowUpUpdate,
    ctx: RequestContext = Depends(require_permission(CUSTOMER_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> s.FollowUpRead:
    follow_up = await _repo(FollowUp)(db, ctx.tenant_id).get(follow_up_id)
    if follow_up.status == "done":
        raise ConflictError("A completed follow-up cannot be changed")
    data = payload.model_dump(exclude_unset=True)
    if "due_at" in data and data["due_at"]:
        follow_up.due_at = svc._aware(data["due_at"])
    if "message" in data:
        follow_up.payload = {**follow_up.payload, "message": data["message"]}
    if data.get("status"):
        follow_up.status = data["status"]
        if data["status"] == "pending":  # retry a failed one
            follow_up.attempts = 0
            follow_up.last_error = None
            follow_up.executed_at = None
    await db.commit()
    await db.refresh(follow_up)
    return (await _enrich_follow_ups(db, ctx.tenant_id, [follow_up]))[0]


@router.post("/follow-ups/{follow_up_id}/run", response_model=s.FollowUpRead)
async def run_follow_up_now(
    follow_up_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(CUSTOMER_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> s.FollowUpRead:
    follow_up = await _repo(FollowUp)(db, ctx.tenant_id).get(follow_up_id)
    if follow_up.status != "pending":
        raise ConflictError(f"Follow-up is {follow_up.status}, not pending")
    await svc.execute_follow_up(db, follow_up)
    await db.commit()
    await db.refresh(follow_up)
    return (await _enrich_follow_ups(db, ctx.tenant_id, [follow_up]))[0]


# ============================== collections =================================


async def _enrich_promises(
    db: AsyncSession, tenant_id: uuid.UUID, promises: list[PaymentPromise]
) -> list[s.PromiseRead]:
    if not promises:
        return []
    names = {
        c.id: c.name
        for c in (
            await db.execute(
                select(Customer).where(
                    Customer.tenant_id == tenant_id,
                    Customer.id.in_({p.customer_id for p in promises}),
                )
            )
        ).scalars()
    }
    today = datetime.now(UTC).date()
    out = []
    for p in promises:
        row = s.PromiseRead.model_validate(p)
        row.customer_name = names.get(p.customer_id)
        row.overdue = svc.is_overdue(p, today)
        row.days_overdue = (today - p.due_date).days if row.overdue else 0
        out.append(row)
    return out


@router.get("/collections/promises", response_model=list[s.PromiseRead])
async def list_promises(
    status_filter: s.PromiseStatus | None = Query(default=None, alias="status"),
    customer_id: uuid.UUID | None = None,
    limit: int = Query(default=200, le=500),
    ctx: RequestContext = Depends(require_permission(PAYMENTS_READ)),
    db: AsyncSession = Depends(get_db),
) -> list[s.PromiseRead]:
    query = select(PaymentPromise).where(PaymentPromise.tenant_id == ctx.tenant_id)
    if status_filter:
        query = query.where(PaymentPromise.status == status_filter)
    if customer_id:
        query = query.where(PaymentPromise.customer_id == customer_id)
    rows = await db.execute(query.order_by(PaymentPromise.due_date).limit(limit))
    return await _enrich_promises(db, ctx.tenant_id, list(rows.scalars().all()))


@router.get("/collections/overdue", response_model=list[s.PromiseRead])
async def list_overdue_promises(
    ctx: RequestContext = Depends(require_permission(PAYMENTS_READ)),
    db: AsyncSession = Depends(get_db),
) -> list[s.PromiseRead]:
    rows = await db.execute(
        select(PaymentPromise)
        .where(
            PaymentPromise.tenant_id == ctx.tenant_id,
            PaymentPromise.status == "pending",
            PaymentPromise.due_date < datetime.now(UTC).date(),
        )
        .order_by(PaymentPromise.due_date)
    )
    return await _enrich_promises(db, ctx.tenant_id, list(rows.scalars().all()))


@router.post("/collections/promises", response_model=s.PromiseRead, status_code=201)
async def create_promise(
    payload: s.PromiseCreate,
    ctx: RequestContext = Depends(require_permission(PAYMENTS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> s.PromiseRead:
    try:
        promise, _ = await svc.create_payment_promise(
            db,
            ctx.tenant_id,
            customer_id=payload.customer_id,
            amount=payload.amount,
            due_date=payload.due_date,
            currency=payload.currency,
            notes=payload.notes,
        )
    except ValueError as exc:
        raise NotFoundError(str(exc)) from exc
    await db.commit()
    return (await _enrich_promises(db, ctx.tenant_id, [promise]))[0]


@router.patch("/collections/promises/{promise_id}", response_model=s.PromiseRead)
async def update_promise(
    promise_id: uuid.UUID,
    payload: s.PromiseUpdate,
    ctx: RequestContext = Depends(require_permission(PAYMENTS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> s.PromiseRead:
    promise = await _repo(PaymentPromise)(db, ctx.tenant_id).get(promise_id)
    data = payload.model_dump(exclude_unset=True)
    for field, value in data.items():
        if value is not None or field == "notes":
            setattr(promise, field, value)
    if promise.status != "pending":
        await svc.cancel_promise_reminders(db, promise.id)
    await db.commit()
    await db.refresh(promise)
    return (await _enrich_promises(db, ctx.tenant_id, [promise]))[0]


@router.post("/collections/promises/{promise_id}/remind", response_model=s.FollowUpRead, status_code=201)
async def remind_promise(
    promise_id: uuid.UUID,
    channel: Literal["call", "whatsapp", "sms", "email"] = "whatsapp",
    ctx: RequestContext = Depends(require_permission(PAYMENTS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> s.FollowUpRead:
    promise = await _repo(PaymentPromise)(db, ctx.tenant_id).get(promise_id)
    if promise.status != "pending":
        raise ConflictError(f"Promise is {promise.status}; nothing to remind")
    customer = await db.get(Customer, promise.customer_id)
    follow_up = await svc.schedule_promise_reminder(
        db, promise, customer, channel=channel, immediate=True
    )
    if follow_up is None:
        raise BadRequest("Customer has no phone number to remind")
    await db.commit()
    await db.refresh(follow_up)
    return (await _enrich_follow_ups(db, ctx.tenant_id, [follow_up]))[0]
