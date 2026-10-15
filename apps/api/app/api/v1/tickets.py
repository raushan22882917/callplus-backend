import uuid

from app.core.deps import RequestContext, get_db, require_permission
from app.core.permissions import Permissions
from app.core.repository import TenantScopedRepository
from app.schemas.ticket import (
    TicketCommentCreate,
    TicketCommentRead,
    TicketCreate,
    TicketRead,
    TicketUpdate,
)
from app.services import csat as csat_service
from app.services.operations import apply_sla_safely, reapply_sla_for_priority
from app.services.webhooks import enqueue_webhook_event
from app.workflows.dispatch import enqueue_workflow_trigger
from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import Ticket, TicketComment, TicketStatus, WorkflowTrigger

router = APIRouter(prefix="/tickets", tags=["tickets"])


class TicketRepository(TenantScopedRepository[Ticket]):
    model = Ticket


class TicketCommentRepository(TenantScopedRepository[TicketComment]):
    model = TicketComment


@router.get("", response_model=list[TicketRead])
async def list_tickets(
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    ctx: RequestContext = Depends(require_permission(Permissions.TICKETS_READ)),
    db: AsyncSession = Depends(get_db),
) -> list[Ticket]:
    return await TicketRepository(db, ctx.tenant_id).list(limit=limit, offset=offset)


@router.post("", response_model=TicketRead, status_code=status.HTTP_201_CREATED)
async def create_ticket(
    payload: TicketCreate,
    ctx: RequestContext = Depends(require_permission(Permissions.TICKETS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> Ticket:
    ticket = await TicketRepository(db, ctx.tenant_id).create(**payload.model_dump())
    await apply_sla_safely(db, ctx.tenant_id, ticket)
    await db.commit()
    await db.refresh(ticket)

    enqueue_workflow_trigger(
        tenant_id=ctx.tenant_id,
        trigger=WorkflowTrigger.TICKET_CREATED,
        payload={
            "ticket_id": str(ticket.id),
            "customer_id": str(ticket.customer_id),
            "priority": ticket.priority.value,
            "source": "manual",
        },
    )
    enqueue_webhook_event(
        tenant_id=ctx.tenant_id,
        event="ticket.created",
        data={
            "ticket_id": str(ticket.id),
            "customer_id": str(ticket.customer_id),
            "subject": ticket.subject,
            "priority": ticket.priority.value,
            "source": "manual",
        },
    )
    return ticket


@router.get("/{ticket_id}", response_model=TicketRead)
async def get_ticket(
    ticket_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(Permissions.TICKETS_READ)),
    db: AsyncSession = Depends(get_db),
) -> Ticket:
    return await TicketRepository(db, ctx.tenant_id).get(ticket_id)


@router.patch("/{ticket_id}", response_model=TicketRead)
async def update_ticket(
    ticket_id: uuid.UUID,
    payload: TicketUpdate,
    ctx: RequestContext = Depends(require_permission(Permissions.TICKETS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> Ticket:
    repo = TicketRepository(db, ctx.tenant_id)
    ticket = await repo.get(ticket_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(ticket, field, value)
    if "priority" in payload.model_fields_set:
        await reapply_sla_for_priority(db, ctx.tenant_id, ticket)
    await db.commit()
    await db.refresh(ticket)
    if payload.status in (TicketStatus.RESOLVED, TicketStatus.CLOSED):
        await csat_service.schedule_after_ticket(db, ticket)
    return ticket


@router.get("/{ticket_id}/comments", response_model=list[TicketCommentRead])
async def list_ticket_comments(
    ticket_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(Permissions.TICKETS_READ)),
    db: AsyncSession = Depends(get_db),
) -> list[TicketComment]:
    await TicketRepository(db, ctx.tenant_id).get(ticket_id)  # 404s if not in this tenant
    result = await db.execute(
        select(TicketComment)
        .where(TicketComment.tenant_id == ctx.tenant_id, TicketComment.ticket_id == ticket_id)
        .order_by(TicketComment.created_at.asc())
    )
    return list(result.scalars().all())


@router.post(
    "/{ticket_id}/comments", response_model=TicketCommentRead, status_code=status.HTTP_201_CREATED
)
async def add_ticket_comment(
    ticket_id: uuid.UUID,
    payload: TicketCommentCreate,
    ctx: RequestContext = Depends(require_permission(Permissions.TICKETS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> TicketComment:
    await TicketRepository(db, ctx.tenant_id).get(ticket_id)  # 404s if not in this tenant
    comment = await TicketCommentRepository(db, ctx.tenant_id).create(
        ticket_id=ticket_id, author_user_id=ctx.user_id, body=payload.body
    )
    await db.commit()
    await db.refresh(comment)
    return comment
