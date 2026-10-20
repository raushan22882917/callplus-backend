"""Super-admin panel. Access = users.is_platform_admin OR a row in
platform_admins. There is deliberately no impersonation."""

import asyncio
import time
import uuid
from typing import Any

from app.core.audit import log_audit_event
from app.core.config import get_settings
from app.core.deps import RequestContext, get_current_context, get_db
from app.core.exceptions import NotFoundError, PermissionDeniedError
from app.core.rate_limit import _redis
from app.schemas.platform import (
    AdminHealth,
    AdminPlanChange,
    AdminStatusChange,
    AdminTenantDetail,
    AdminTenantRow,
)
from app.services import plans
from app.services.platform_usage import usage_summary
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import (
    Call,
    PhoneNumber,
    Role,
    Tenant,
    TenantStatus,
    TenantUser,
    User,
)
from packages.database.models.platform import PlatformAdmin

router = APIRouter(prefix="/admin")


async def is_super_admin(db: AsyncSession, ctx: RequestContext) -> bool:
    if ctx.api_key_id is not None:
        return False
    if ctx.is_platform_admin:
        return True
    return (
        await db.execute(select(PlatformAdmin.id).where(PlatformAdmin.user_id == ctx.user_id))
    ).scalar_one_or_none() is not None


async def require_super_admin(
    ctx: RequestContext = Depends(get_current_context), db: AsyncSession = Depends(get_db)
) -> RequestContext:
    if not await is_super_admin(db, ctx):
        raise PermissionDeniedError("This area is restricted to platform administrators")
    # Cross-tenant work: lift the tenant row-level-security scope that
    # get_current_context applied for ordinary members.
    db.sync_session.info.pop("tenant_id", None)
    await db.execute(text("SELECT set_config('app.tenant_id', '', true)"))
    return ctx


@router.get("/me")
async def admin_me(
    ctx: RequestContext = Depends(get_current_context), db: AsyncSession = Depends(get_db)
) -> dict[str, bool]:
    """Lets the frontend decide whether to show the admin area."""
    return {"is_platform_admin": await is_super_admin(db, ctx)}


async def _row(db: AsyncSession, tenant: Tenant) -> AdminTenantRow:
    users = (
        await db.execute(select(func.count(TenantUser.id)).where(TenantUser.tenant_id == tenant.id))
    ).scalar_one()
    numbers = (
        await db.execute(
            select(func.count(PhoneNumber.id)).where(PhoneNumber.tenant_id == tenant.id)
        )
    ).scalar_one()
    usage = await usage_summary(db, tenant.id)
    calls = int(
        (
            await db.execute(
                select(func.count(Call.id)).where(
                    Call.tenant_id == tenant.id,
                    func.to_char(func.coalesce(Call.started_at, Call.created_at), "YYYY-MM")
                    == usage.month,
                )
            )
        ).scalar_one()
    )
    return AdminTenantRow(
        id=tenant.id,
        name=tenant.name,
        slug=tenant.slug,
        status=tenant.status.value,
        plan_tier=tenant.plan_tier,
        created_at=tenant.created_at,
        users=users,
        phone_numbers=numbers,
        calls_month=calls,
        usage_cost_month=usage.estimated_total,
        currency=usage.currency,
    )


@router.get("/tenants", response_model=list[AdminTenantRow])
async def list_tenants(
    q: str | None = Query(default=None, max_length=100),
    tenant_status: TenantStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    _: RequestContext = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
) -> list[AdminTenantRow]:
    query = select(Tenant)
    if q:
        like = f"%{q.strip()}%"
        query = query.where(Tenant.name.ilike(like) | Tenant.slug.ilike(like))
    if tenant_status is not None:
        query = query.where(Tenant.status == tenant_status)
    tenants = (
        (await db.execute(query.order_by(Tenant.created_at.desc()).limit(limit).offset(offset)))
        .scalars()
        .all()
    )
    return [await _row(db, t) for t in tenants]


async def _tenant(db: AsyncSession, tenant_id: uuid.UUID) -> Tenant:
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        raise NotFoundError("Tenant not found")
    return tenant


@router.get("/tenants/{tenant_id}", response_model=AdminTenantDetail)
async def tenant_detail(
    tenant_id: uuid.UUID,
    _: RequestContext = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminTenantDetail:
    tenant = await _tenant(db, tenant_id)
    row = await _row(db, tenant)
    members = (
        await db.execute(
            select(User.id, User.email, User.full_name, User.is_active, Role.name)
            .join(TenantUser, TenantUser.user_id == User.id)
            .join(Role, Role.id == TenantUser.role_id)
            .where(TenantUser.tenant_id == tenant_id)
            .order_by(User.email)
        )
    ).all()
    numbers = (
        (await db.execute(select(PhoneNumber).where(PhoneNumber.tenant_id == tenant_id)))
        .scalars()
        .all()
    )
    return AdminTenantDetail(
        **row.model_dump(),
        user_list=[
            {
                "id": str(m.id),
                "email": m.email,
                "full_name": m.full_name,
                "is_active": m.is_active,
                "role": m.name,
            }
            for m in members
        ],
        number_list=[
            {"id": str(n.id), "e164_number": n.e164_number, "provider": n.provider} for n in numbers
        ],
        usage=await usage_summary(db, tenant_id),
    )


async def _set_status(
    db: AsyncSession,
    ctx: RequestContext,
    tenant_id: uuid.UUID,
    new: TenantStatus,
    reason: str | None,
) -> AdminTenantRow:
    tenant = await _tenant(db, tenant_id)
    if tenant_id == ctx.tenant_id and new == TenantStatus.SUSPENDED:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You cannot suspend your own workspace")
    previous = tenant.status
    tenant.status = new
    await log_audit_event(
        db,
        tenant_id=tenant_id,
        actor_user_id=ctx.user_id,
        action=f"tenant.{new.value}",
        resource_type="tenant",
        resource_id=tenant_id,
        extra={"from": previous.value, "reason": reason, "by_platform_admin": True},
    )
    await db.commit()
    return await _row(db, tenant)


@router.post("/tenants/{tenant_id}/suspend", response_model=AdminTenantRow)
async def suspend_tenant(
    tenant_id: uuid.UUID,
    payload: AdminStatusChange | None = None,
    ctx: RequestContext = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminTenantRow:
    return await _set_status(
        db, ctx, tenant_id, TenantStatus.SUSPENDED, payload.reason if payload else None
    )


@router.post("/tenants/{tenant_id}/activate", response_model=AdminTenantRow)
async def activate_tenant(
    tenant_id: uuid.UUID,
    payload: AdminStatusChange | None = None,
    ctx: RequestContext = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminTenantRow:
    return await _set_status(
        db, ctx, tenant_id, TenantStatus.ACTIVE, payload.reason if payload else None
    )


@router.put("/tenants/{tenant_id}/plan", response_model=AdminTenantRow)
async def set_tenant_plan(
    tenant_id: uuid.UUID,
    payload: AdminPlanChange,
    ctx: RequestContext = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminTenantRow:
    """Moves a workspace to a plan and/or grants extras (more minutes, overage).
    Platform-admin only: tenants can never change their own entitlements."""
    if payload.tier not in plans.TIERS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown plan '{payload.tier}'")
    try:
        overrides = plans.sanitize_overrides(payload.overrides)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    tenant = await _tenant(db, tenant_id)
    previous = {"tier": tenant.plan_tier, "overrides": tenant.plan_overrides}
    tenant.plan_tier = payload.tier
    tenant.plan_overrides = overrides
    await log_audit_event(
        db,
        tenant_id=tenant_id,
        actor_user_id=ctx.user_id,
        action="plan.set_by_admin",
        resource_type="tenant",
        resource_id=tenant_id,
        extra={
            "from": previous,
            "to": {"tier": payload.tier, "overrides": overrides},
            "reason": payload.reason,
            "by_platform_admin": True,
        },
    )
    await db.commit()
    return await _row(db, tenant)


async def _ping_db(db: AsyncSession) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        await db.execute(text("SELECT 1"))
        return {"ok": True, "latency_ms": round((time.perf_counter() - started) * 1000, 1)}
    except Exception as exc:
        return {"ok": False, "error": exc.__class__.__name__}


async def _ping_redis() -> dict[str, Any]:
    started = time.perf_counter()
    try:
        await _redis().ping()
        return {"ok": True, "latency_ms": round((time.perf_counter() - started) * 1000, 1)}
    except Exception as exc:
        return {"ok": False, "error": exc.__class__.__name__}


async def _ping_celery() -> dict[str, Any]:
    def ping() -> dict[str, Any]:
        from app.worker.celery_app import celery_app

        replies = celery_app.control.inspect(timeout=1.0).ping() or {}
        return {"ok": bool(replies), "workers": sorted(replies.keys())}

    try:
        return await asyncio.wait_for(asyncio.to_thread(ping), timeout=5)
    except Exception as exc:
        return {"ok": False, "workers": [], "error": exc.__class__.__name__}


@router.get("/health", response_model=AdminHealth)
async def system_health(
    request: Request,
    _: RequestContext = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminHealth:
    database, redis, celery = await asyncio.gather(_ping_db(db), _ping_redis(), _ping_celery())
    return AdminHealth(
        version=request.app.version,
        environment=get_settings().env,
        database=database,
        redis=redis,
        celery=celery,
    )
