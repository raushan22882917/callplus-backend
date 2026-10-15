import uuid

from app.core.audit import log_audit_event
from app.core.deps import RequestContext, get_db, require_permission
from app.core.permissions import Permissions
from app.core.repository import TenantScopedRepository
from app.schemas.transfer_target import (
    TransferTargetCreate,
    TransferTargetRead,
    TransferTargetUpdate,
)
from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import TransferTarget

router = APIRouter(prefix="/transfer-targets", tags=["transfer-targets"])


class TransferTargetRepository(TenantScopedRepository[TransferTarget]):
    model = TransferTarget


@router.get("", response_model=list[TransferTargetRead])
async def list_transfer_targets(
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    ctx: RequestContext = Depends(require_permission(Permissions.AGENTS_READ)),
    db: AsyncSession = Depends(get_db),
) -> list[TransferTarget]:
    return await TransferTargetRepository(db, ctx.tenant_id).list(limit=limit, offset=offset)


@router.post("", response_model=TransferTargetRead, status_code=status.HTTP_201_CREATED)
async def create_transfer_target(
    payload: TransferTargetCreate,
    ctx: RequestContext = Depends(require_permission(Permissions.AGENTS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> TransferTarget:
    target = await TransferTargetRepository(db, ctx.tenant_id).create(**payload.model_dump())
    await log_audit_event(
        db,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        action="transfer_target.created",
        resource_type="transfer_target",
        resource_id=target.id,
        extra={"name": target.name},
    )
    await db.commit()
    await db.refresh(target)
    return target


@router.patch("/{target_id}", response_model=TransferTargetRead)
async def update_transfer_target(
    target_id: uuid.UUID,
    payload: TransferTargetUpdate,
    ctx: RequestContext = Depends(require_permission(Permissions.AGENTS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> TransferTarget:
    target = await TransferTargetRepository(db, ctx.tenant_id).get(target_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(target, field, value)
    await db.commit()
    await db.refresh(target)
    return target


@router.delete("/{target_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_transfer_target(
    target_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(Permissions.AGENTS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> None:
    await TransferTargetRepository(db, ctx.tenant_id).delete(target_id)
    await log_audit_event(
        db,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        action="transfer_target.deleted",
        resource_type="transfer_target",
        resource_id=target_id,
    )
    await db.commit()
