import uuid

from app.core.audit import log_audit_event
from app.core.deps import RequestContext, get_db, require_permission
from app.core.permissions import Permissions
from app.core.repository import TenantScopedRepository
from app.schemas.customer import CustomerCreate, CustomerDossier, CustomerRead, CustomerUpdate
from app.services.platform_masking import masked_customers
from app.workflows.dispatch import enqueue_workflow_trigger
from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import (
    Call,
    ConsentRecord,
    Conversation,
    ConversationChannel,
    Customer,
    Message,
    Ticket,
    WorkflowTrigger,
)

router = APIRouter(prefix="/customers", tags=["customers"])


class CustomerRepository(TenantScopedRepository[Customer]):
    model = Customer


@router.get("", response_model=list[CustomerRead])
async def list_customers(
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    ctx: RequestContext = Depends(require_permission(Permissions.CUSTOMERS_READ)),
    db: AsyncSession = Depends(get_db),
) -> list[CustomerRead]:
    rows = await CustomerRepository(db, ctx.tenant_id).list(limit=limit, offset=offset)
    return await masked_customers(db, ctx, rows)


@router.post("", response_model=CustomerRead, status_code=status.HTTP_201_CREATED)
async def create_customer(
    payload: CustomerCreate,
    ctx: RequestContext = Depends(require_permission(Permissions.CUSTOMERS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> Customer:
    customer = await CustomerRepository(db, ctx.tenant_id).create(**payload.model_dump())
    await db.commit()
    await db.refresh(customer)

    enqueue_workflow_trigger(
        tenant_id=ctx.tenant_id,
        trigger=WorkflowTrigger.CUSTOMER_CREATED,
        payload={"customer_id": str(customer.id), "phone": customer.phone, "source": "manual"},
    )
    return customer


@router.get("/{customer_id}/dossier", response_model=CustomerDossier)
async def customer_dossier(
    customer_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(Permissions.CUSTOMERS_READ)),
    db: AsyncSession = Depends(get_db),
) -> CustomerDossier:
    """Calls, messages, tickets and consent for this one person."""
    await CustomerRepository(db, ctx.tenant_id).get(customer_id)
    calls = (
        await db.execute(
            select(Call)
            .where(Call.tenant_id == ctx.tenant_id, Call.customer_id == customer_id)
            .order_by(Call.created_at.desc())
            .limit(30)
        )
    ).scalars().all()
    messages = (
        await db.execute(
            select(Message, Conversation.channel)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Message.tenant_id == ctx.tenant_id,
                Conversation.customer_id == customer_id,
                Conversation.channel != ConversationChannel.INTERNAL,
            )
            .order_by(Message.created_at.desc())
            .limit(40)
        )
    ).all()
    tickets = (
        await db.execute(
            select(Ticket)
            .where(Ticket.tenant_id == ctx.tenant_id, Ticket.customer_id == customer_id)
            .order_by(Ticket.created_at.desc())
            .limit(20)
        )
    ).scalars().all()
    consents = (
        await db.execute(
            select(ConsentRecord)
            .where(ConsentRecord.tenant_id == ctx.tenant_id, ConsentRecord.customer_id == customer_id)
            .order_by(ConsentRecord.granted_at.desc())
            .limit(20)
        )
    ).scalars().all()
    return CustomerDossier(
        calls=[
            {
                "id": c.id,
                "direction": c.direction.value,
                "status": c.status.value,
                "duration_seconds": c.duration_seconds,
                "created_at": c.created_at,
            }
            for c in calls
        ],
        messages=[
            {
                "id": m.id,
                "channel": channel.value,
                "direction": m.direction.value,
                "text": (m.text or "")[:240] or None,
                "created_at": m.created_at,
            }
            for m, channel in messages
        ],
        tickets=[
            {
                "id": t.id,
                "subject": t.subject,
                "status": t.status.value,
                "priority": t.priority.value,
                "created_at": t.created_at,
            }
            for t in tickets
        ],
        consents=[
            {
                "id": row.id,
                "purpose": row.purpose,
                "status": row.status,
                "source": row.source,
                "granted_at": row.granted_at,
                "withdrawn_at": row.withdrawn_at,
            }
            for row in consents
        ],
    )


@router.get("/{customer_id}", response_model=CustomerRead)
async def get_customer(
    customer_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(Permissions.CUSTOMERS_READ)),
    db: AsyncSession = Depends(get_db),
) -> CustomerRead:
    customer = await CustomerRepository(db, ctx.tenant_id).get(customer_id)
    return (await masked_customers(db, ctx, [customer]))[0]


@router.patch("/{customer_id}", response_model=CustomerRead)
async def update_customer(
    customer_id: uuid.UUID,
    payload: CustomerUpdate,
    ctx: RequestContext = Depends(require_permission(Permissions.CUSTOMERS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> Customer:
    repo = CustomerRepository(db, ctx.tenant_id)
    customer = await repo.get(customer_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(customer, field, value)
    await db.commit()
    await db.refresh(customer)
    return customer


@router.delete("/{customer_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_customer(
    customer_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(Permissions.CUSTOMERS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> None:
    repo = CustomerRepository(db, ctx.tenant_id)
    await repo.get(customer_id)  # 404s if not in this tenant
    await log_audit_event(
        db,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        action="customer.deleted",
        resource_type="customer",
        resource_id=customer_id,
    )
    await repo.delete(customer_id)
    await db.commit()
