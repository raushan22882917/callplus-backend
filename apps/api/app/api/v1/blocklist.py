"""Manual management of blocked callers. See app/api/v1/telephony.py for
where a number gets added here automatically (spam protection) and where
this list is enforced (rejecting a call before it ever creates a Call row)."""

import uuid

from app.core.audit import log_audit_event
from app.core.deps import RequestContext, get_db, require_permission
from app.core.permissions import Permissions
from app.core.repository import TenantScopedRepository
from app.schemas.blocklist import BlockedCallerCreate, BlockedCallerRead
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import BlockedCaller

router = APIRouter(prefix="/blocked-callers", tags=["blocked-callers"])


class BlockedCallerRepository(TenantScopedRepository[BlockedCaller]):
    model = BlockedCaller


@router.get("", response_model=list[BlockedCallerRead])
async def list_blocked_callers(
    ctx: RequestContext = Depends(require_permission(Permissions.CALLS_READ)),
    db: AsyncSession = Depends(get_db),
) -> list[BlockedCaller]:
    return await BlockedCallerRepository(db, ctx.tenant_id).list(limit=500)


@router.post("", response_model=BlockedCallerRead, status_code=status.HTTP_201_CREATED)
async def block_caller(
    payload: BlockedCallerCreate,
    ctx: RequestContext = Depends(require_permission(Permissions.CALLS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> BlockedCaller:
    existing = await db.execute(
        select(BlockedCaller).where(
            BlockedCaller.tenant_id == ctx.tenant_id,
            BlockedCaller.e164_number == payload.e164_number,
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Number is already blocked")

    blocked = await BlockedCallerRepository(db, ctx.tenant_id).create(**payload.model_dump())
    await log_audit_event(
        db,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        action="caller.blocked",
        resource_type="blocked_caller",
        resource_id=blocked.id,
        extra={"e164_number": payload.e164_number},
    )
    await db.commit()
    await db.refresh(blocked)
    return blocked


@router.delete("/{blocked_caller_id}", status_code=status.HTTP_204_NO_CONTENT)
async def unblock_caller(
    blocked_caller_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(Permissions.CALLS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> None:
    repo = BlockedCallerRepository(db, ctx.tenant_id)
    await repo.get(blocked_caller_id)  # 404s if not in this tenant
    await log_audit_event(
        db,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        action="caller.unblocked",
        resource_type="blocked_caller",
        resource_id=blocked_caller_id,
    )
    await repo.delete(blocked_caller_id)
    await db.commit()
