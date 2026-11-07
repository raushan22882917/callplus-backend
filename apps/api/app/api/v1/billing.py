import uuid

from app.core.audit import log_audit_event
from app.core.deps import RequestContext, get_db, require_permission, require_platform_admin
from app.core.exceptions import NotFoundError
from app.core.permissions import Permissions
from app.core.repository import TenantScopedRepository
from app.integrations.razorpay_client import RazorpayAPIError
from app.integrations.razorpay_client import cancel_subscription as razorpay_cancel_subscription
from app.integrations.razorpay_client import create_subscription as razorpay_create_subscription
from app.schemas.billing import (
    CancelSubscriptionRequest,
    CreateSubscriptionRequest,
    InvoiceRead,
    PlanAdminRead,
    PlanCreate,
    PlanRead,
    PlanUpdate,
    SubscriptionRead,
)
from app.services import plans as plans_service
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import Invoice, Plan, Subscription, SubscriptionStatus, Tenant

router = APIRouter(prefix="/billing", tags=["billing"])


class SubscriptionRepository(TenantScopedRepository[Subscription]):
    model = Subscription


class InvoiceRepository(TenantScopedRepository[Invoice]):
    model = Invoice


@router.get("/entitlements")
async def get_entitlements(
    ctx: RequestContext = Depends(require_permission(Permissions.BILLING_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """The workspace's plan and how much of each allowance is used. Minutes are
    billable minutes, rounded up per call the way the carrier bills."""
    tenant = await db.get(Tenant, ctx.tenant_id)
    snapshot = await plans_service.entitlements(db, tenant)
    await db.commit()
    return snapshot


@router.get("/catalogue")
async def get_catalogue(
    ctx: RequestContext = Depends(require_permission(Permissions.BILLING_MANAGE)),
) -> list[dict]:
    """Every plan with its price and allowances, for the upgrade screen."""
    return plans_service.public_catalogue()


@router.get("/plans", response_model=list[PlanRead])
async def list_plans(
    ctx: RequestContext = Depends(require_permission(Permissions.BILLING_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> list[Plan]:
    result = await db.execute(
        select(Plan).where(Plan.is_active.is_(True)).order_by(Plan.price_paise)
    )
    return list(result.scalars().all())


@router.get("/subscription", response_model=SubscriptionRead)
async def get_subscription(
    ctx: RequestContext = Depends(require_permission(Permissions.BILLING_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> Subscription:
    result = await db.execute(
        select(Subscription)
        .where(Subscription.tenant_id == ctx.tenant_id)
        .order_by(Subscription.created_at.desc())
        .limit(1)
    )
    subscription = result.scalar_one_or_none()
    if subscription is None:
        raise NotFoundError("This tenant has no subscription yet")
    return subscription


@router.post("/subscription", response_model=SubscriptionRead, status_code=status.HTTP_201_CREATED)
async def create_subscription(
    payload: CreateSubscriptionRequest,
    ctx: RequestContext = Depends(require_permission(Permissions.BILLING_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> Subscription:
    existing = await db.execute(
        select(Subscription).where(
            Subscription.tenant_id == ctx.tenant_id,
            Subscription.status.in_([SubscriptionStatus.CREATED, SubscriptionStatus.ACTIVE]),
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Tenant already has an active subscription")

    plan = (
        await db.execute(
            select(Plan).where(Plan.code == payload.plan_code, Plan.is_active.is_(True))
        )
    ).scalar_one_or_none()
    if plan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown plan")
    if not plan.provider_plan_id:
        raise HTTPException(
            status.HTTP_424_FAILED_DEPENDENCY,
            "This plan is not yet connected to a Razorpay plan (provider_plan_id is unset)",
        )

    try:
        response = await razorpay_create_subscription(
            plan_id=plan.provider_plan_id, total_count=120
        )
    except RazorpayAPIError as exc:
        raise HTTPException(status.HTTP_424_FAILED_DEPENDENCY, str(exc)) from exc

    subscription = await SubscriptionRepository(db, ctx.tenant_id).create(
        plan_id=plan.id,
        provider_subscription_id=response["id"],
        status=SubscriptionStatus.CREATED,
    )
    await log_audit_event(
        db,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        action="subscription.created",
        resource_type="subscription",
        resource_id=subscription.id,
        extra={"plan_code": plan.code},
    )
    await db.commit()
    await db.refresh(subscription)
    return subscription


@router.post("/subscription/cancel", response_model=SubscriptionRead)
async def cancel_subscription(
    payload: CancelSubscriptionRequest,
    ctx: RequestContext = Depends(require_permission(Permissions.BILLING_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> Subscription:
    result = await db.execute(
        select(Subscription)
        .where(Subscription.tenant_id == ctx.tenant_id)
        .order_by(Subscription.created_at.desc())
        .limit(1)
    )
    subscription = result.scalar_one_or_none()
    if subscription is None or subscription.status not in (
        SubscriptionStatus.CREATED,
        SubscriptionStatus.ACTIVE,
    ):
        raise HTTPException(status.HTTP_409_CONFLICT, "Tenant has no active subscription to cancel")

    try:
        await razorpay_cancel_subscription(
            subscription.provider_subscription_id, cancel_at_cycle_end=payload.at_period_end
        )
    except RazorpayAPIError as exc:
        raise HTTPException(status.HTTP_424_FAILED_DEPENDENCY, str(exc)) from exc

    if payload.at_period_end:
        subscription.cancel_at_period_end = True
    else:
        subscription.status = SubscriptionStatus.CANCELLED
        # The webhook would do this later. Doing it here means a missed webhook
        # cannot leave a cancelled workspace on the paid tier.
        from app.api.v1.payments import _sync_tenant_tier

        await _sync_tenant_tier(db, subscription, "subscription.cancelled")

    await log_audit_event(
        db,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        action="subscription.cancelled",
        resource_type="subscription",
        resource_id=subscription.id,
        extra={"at_period_end": payload.at_period_end},
    )
    await db.commit()
    await db.refresh(subscription)
    return subscription


@router.get("/invoices", response_model=list[InvoiceRead])
async def list_invoices(
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    ctx: RequestContext = Depends(require_permission(Permissions.BILLING_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> list[Invoice]:
    return await InvoiceRepository(db, ctx.tenant_id).list(limit=limit, offset=offset)


# --- Platform-admin plan catalog management ---
#
# Plan is a shared, platform-wide catalog (not tenant-scoped — see the model
# docstring), so these endpoints are gated on is_platform_admin rather than
# the tenant-scoped billing.manage permission: a tenant's own billing admin
# must not be able to edit the price/currency every other tenant sees.


@router.get("/admin/plans", response_model=list[PlanAdminRead])
async def admin_list_plans(
    ctx: RequestContext = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_db),
) -> list[Plan]:
    result = await db.execute(select(Plan).order_by(Plan.currency, Plan.price_paise))
    return list(result.scalars().all())


@router.post("/admin/plans", response_model=PlanAdminRead, status_code=status.HTTP_201_CREATED)
async def admin_create_plan(
    payload: PlanCreate,
    ctx: RequestContext = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_db),
) -> Plan:
    existing = await db.execute(select(Plan).where(Plan.code == payload.code))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "A plan with this code already exists")

    plan = Plan(**payload.model_dump())
    db.add(plan)
    await log_audit_event(
        db,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        action="plan.created",
        resource_type="plan",
        extra={"code": payload.code},
    )
    await db.commit()
    await db.refresh(plan)
    return plan


@router.patch("/admin/plans/{plan_id}", response_model=PlanAdminRead)
async def admin_update_plan(
    plan_id: uuid.UUID,
    payload: PlanUpdate,
    ctx: RequestContext = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_db),
) -> Plan:
    plan = await db.get(Plan, plan_id)
    if plan is None:
        raise NotFoundError("Plan not found")

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(plan, field, value)

    await log_audit_event(
        db,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        action="plan.updated",
        resource_type="plan",
        resource_id=plan.id,
        extra=payload.model_dump(exclude_unset=True),
    )
    await db.commit()
    await db.refresh(plan)
    return plan
