from app.core.audit import log_audit_event
from app.core.deps import RequestContext, get_db, require_permission
from app.core.permissions import Permissions
from app.schemas.post_call import (
    CallNotificationRead,
    PostCallSettingsRead,
    PostCallSettingsUpdate,
)
from app.services import post_call as svc
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models.post_call import CallNotification, PostCallSettings

router = APIRouter(prefix="/post-call", tags=["post-call"])


@router.get("/settings", response_model=PostCallSettingsRead)
async def get_post_call_settings(
    ctx: RequestContext = Depends(require_permission(Permissions.ANALYTICS_READ)),
    db: AsyncSession = Depends(get_db),
) -> PostCallSettings:
    row = await svc.get_settings_row(db, ctx.tenant_id)
    await db.commit()
    return row


@router.patch("/settings", response_model=PostCallSettingsRead)
async def update_post_call_settings(
    payload: PostCallSettingsUpdate,
    ctx: RequestContext = Depends(require_permission(Permissions.SETTINGS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> PostCallSettings:
    row = await svc.get_settings_row(db, ctx.tenant_id)
    changes = {k: v for k, v in payload.model_dump(exclude_unset=True).items() if v is not None}
    for field, value in changes.items():
        setattr(row, field, value)
    await log_audit_event(
        db,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        action="post_call.update",
        resource_type="post_call_settings",
        resource_id=row.id,
        extra={"fields": sorted(changes)},
    )
    await db.commit()
    await db.refresh(row)
    return row


@router.get("/log", response_model=list[CallNotificationRead])
async def list_post_call_log(
    limit: int = Query(default=30, ge=1, le=100),
    status: str | None = Query(default=None, pattern="^(sent|failed|skipped|pending)$"),
    ctx: RequestContext = Depends(require_permission(Permissions.ANALYTICS_READ)),
    db: AsyncSession = Depends(get_db),
) -> list[CallNotification]:
    query = select(CallNotification).where(CallNotification.tenant_id == ctx.tenant_id)
    if status:
        query = query.where(CallNotification.status == status)
    rows = await db.execute(query.order_by(CallNotification.created_at.desc()).limit(limit))
    return list(rows.scalars().all())
