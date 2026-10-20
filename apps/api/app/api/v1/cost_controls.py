from app.core.audit import log_audit_event
from app.core.deps import RequestContext, get_db, require_permission
from app.core.permissions import Permissions
from app.schemas.cost_controls import (
    CostAlertRead,
    CostControlsRead,
    CostControlsUpdate,
    CostStatusRead,
)
from app.services import cost_controls as svc
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models.cost_controls import CostAlert, CostControls

router = APIRouter(prefix="/cost-controls", tags=["cost-controls"])

# Changing these affects whether calls can be placed, so they follow billing access.
_READ = Permissions.ANALYTICS_READ
_WRITE = Permissions.BILLING_MANAGE


@router.get("", response_model=CostControlsRead)
async def get_cost_controls(
    ctx: RequestContext = Depends(require_permission(_READ)),
    db: AsyncSession = Depends(get_db),
) -> CostControls:
    row = await svc.get_controls(db, ctx.tenant_id)
    await db.commit()
    return row


@router.patch("", response_model=CostControlsRead)
async def update_cost_controls(
    payload: CostControlsUpdate,
    ctx: RequestContext = Depends(require_permission(_WRITE)),
    db: AsyncSession = Depends(get_db),
) -> CostControls:
    row = await svc.get_controls(db, ctx.tenant_id)
    changes = payload.model_dump(exclude_unset=True)
    nullable = {"monthly_budget", "daily_outbound_call_cap", "max_concurrent_outbound"}
    for field, value in changes.items():
        if value is None and field not in nullable:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"{field} cannot be empty")
        setattr(row, field, value)
    if "monthly_budget" in changes:
        # A new budget starts a fresh round of alerts.
        row.alerted_level = 0
    await log_audit_event(
        db,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        action="cost_controls.update",
        resource_type="cost_controls",
        resource_id=row.id,
        extra={"fields": sorted(changes)},
    )
    await db.commit()
    await db.refresh(row)
    await svc.evaluate_alerts_safely(db, ctx.tenant_id)
    return row


@router.get("/status", response_model=CostStatusRead)
async def get_cost_status(
    ctx: RequestContext = Depends(require_permission(_READ)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    snapshot = await svc.status_snapshot(db, ctx.tenant_id)
    await db.commit()
    return snapshot


@router.get("/alerts", response_model=list[CostAlertRead])
async def list_cost_alerts(
    limit: int = Query(default=20, ge=1, le=100),
    ctx: RequestContext = Depends(require_permission(_READ)),
    db: AsyncSession = Depends(get_db),
) -> list[CostAlert]:
    rows = await db.execute(
        select(CostAlert)
        .where(CostAlert.tenant_id == ctx.tenant_id)
        .order_by(CostAlert.created_at.desc())
        .limit(limit)
    )
    return list(rows.scalars().all())


@router.get("/breakdown")
async def get_cost_breakdown(
    month: str | None = Query(default=None, description="YYYY-MM; defaults to this month"),
    ctx: RequestContext = Depends(require_permission(_READ)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        return await svc.cost_breakdown(db, ctx.tenant_id, month)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
