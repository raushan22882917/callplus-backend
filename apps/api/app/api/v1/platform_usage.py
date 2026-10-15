"""Usage metering, itemized estimate, pricing plans and limit checks."""

import uuid
from datetime import UTC, datetime

from app.core.audit import log_audit_event
from app.core.deps import RequestContext, get_db, require_permission
from app.core.exceptions import NotFoundError
from app.core.permissions import Permissions
from app.schemas.platform import (
    METRICS,
    LimitCheck,
    PricingPlanCreate,
    PricingPlanRead,
    PricingPlanUpdate,
    UsageEventCreate,
    UsageEventRead,
    UsageSummary,
)
from app.services.platform_usage import (
    PlanInUse,
    activate_plan,
    check_plan_limit,
    usage_summary,
)
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models.platform import PricingPlan, UsageEvent

router = APIRouter()


@router.get("/usage", response_model=UsageSummary)
async def get_usage(
    month: str | None = Query(default=None, description="YYYY-MM; defaults to this month (UTC)"),
    ctx: RequestContext = Depends(require_permission(Permissions.ANALYTICS_READ)),
    db: AsyncSession = Depends(get_db),
) -> UsageSummary:
    try:
        return await usage_summary(db, ctx.tenant_id, month)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.get("/usage/limits", response_model=list[LimitCheck])
async def get_limits(
    ctx: RequestContext = Depends(require_permission(Permissions.ANALYTICS_READ)),
    db: AsyncSession = Depends(get_db),
) -> list[LimitCheck]:
    return [await check_plan_limit(db, ctx.tenant_id, metric) for metric in METRICS]


@router.get("/usage/events", response_model=list[UsageEventRead])
async def list_usage_events(
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    ctx: RequestContext = Depends(require_permission(Permissions.ANALYTICS_READ)),
    db: AsyncSession = Depends(get_db),
) -> list[UsageEvent]:
    result = await db.execute(
        select(UsageEvent)
        .where(UsageEvent.tenant_id == ctx.tenant_id)
        .order_by(UsageEvent.occurred_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars().all())


@router.post("/usage/events", response_model=UsageEventRead, status_code=status.HTTP_201_CREATED)
async def record_usage_event(
    payload: UsageEventCreate,
    ctx: RequestContext = Depends(require_permission(Permissions.BILLING_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> UsageEvent:
    """Record SMS / storage usage, or an adjustment to any metric (negative
    quantities allowed)."""
    event = UsageEvent(
        tenant_id=ctx.tenant_id,
        metric=payload.metric,
        quantity=payload.quantity,
        occurred_at=payload.occurred_at or datetime.now(UTC),
        reference_id=payload.reference_id,
        extra=payload.extra,
    )
    db.add(event)
    await db.commit()
    await db.refresh(event)
    return event


# ---- pricing plans ---------------------------------------------------------------


async def _plan(db: AsyncSession, tenant_id: uuid.UUID, plan_id: uuid.UUID) -> PricingPlan:
    plan = (
        await db.execute(
            select(PricingPlan).where(PricingPlan.id == plan_id, PricingPlan.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    if plan is None:
        raise NotFoundError("Pricing plan not found")
    return plan


@router.get("/pricing-plans", response_model=list[PricingPlanRead])
async def list_pricing_plans(
    ctx: RequestContext = Depends(require_permission(Permissions.ANALYTICS_READ)),
    db: AsyncSession = Depends(get_db),
) -> list[PricingPlan]:
    result = await db.execute(
        select(PricingPlan)
        .where(PricingPlan.tenant_id == ctx.tenant_id)
        .order_by(PricingPlan.created_at.desc())
    )
    return list(result.scalars().all())


@router.post("/pricing-plans", response_model=PricingPlanRead, status_code=status.HTTP_201_CREATED)
async def create_pricing_plan(
    payload: PricingPlanCreate,
    ctx: RequestContext = Depends(require_permission(Permissions.BILLING_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> PricingPlan:
    plan = PricingPlan(
        tenant_id=ctx.tenant_id,
        name=payload.name,
        currency=payload.currency.upper(),
        rates=payload.rates,
        limits=payload.limits,
        base_fee=payload.base_fee,
        is_active=False,
    )
    db.add(plan)
    await db.flush()
    if payload.is_active:
        await activate_plan(db, plan)
    await log_audit_event(
        db,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        action="pricing_plan.created",
        resource_type="pricing_plan",
        resource_id=plan.id,
    )
    await db.commit()
    await db.refresh(plan)
    return plan


@router.patch("/pricing-plans/{plan_id}", response_model=PricingPlanRead)
async def update_pricing_plan(
    plan_id: uuid.UUID,
    payload: PricingPlanUpdate,
    ctx: RequestContext = Depends(require_permission(Permissions.BILLING_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> PricingPlan:
    plan = await _plan(db, ctx.tenant_id, plan_id)
    fields = payload.model_dump(exclude_unset=True)
    if "rates" in fields or "limits" in fields:
        # Re-run metric validation on partial updates.
        try:
            PricingPlanCreate(
                name=plan.name,
                rates=fields.get("rates", plan.rates),
                limits=fields.get("limits", plan.limits),
            )
        except ValidationError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    activate = fields.pop("is_active", None)
    for key, value in fields.items():
        setattr(plan, key, value.upper() if key == "currency" and value else value)
    if activate is True:
        await activate_plan(db, plan)
    elif activate is False:
        plan.is_active = False
    await db.commit()
    await db.refresh(plan)
    return plan


@router.delete("/pricing-plans/{plan_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_pricing_plan(
    plan_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(Permissions.BILLING_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> None:
    plan = await _plan(db, ctx.tenant_id, plan_id)
    if plan.is_active:
        raise PlanInUse()
    await db.delete(plan)
    await log_audit_event(
        db,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        action="pricing_plan.deleted",
        resource_type="pricing_plan",
        resource_id=plan_id,
    )
    await db.commit()
