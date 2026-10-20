from app.core.deps import RequestContext, get_current_context, get_db, require_permission
from app.core.permissions import Permissions
from app.schemas.tenant import TenantRead, TenantUpdate
from app.services import plans
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import Tenant

router = APIRouter(prefix="/tenants", tags=["tenants"])


@router.get("/me", response_model=TenantRead)
async def get_my_tenant(
    ctx: RequestContext = Depends(get_current_context),
    db: AsyncSession = Depends(get_db),
) -> Tenant:
    return await db.get(Tenant, ctx.tenant_id)


@router.patch("/me", response_model=TenantRead)
async def update_my_tenant(
    payload: TenantUpdate,
    ctx: RequestContext = Depends(require_permission(Permissions.SETTINGS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> Tenant:
    tenant = await db.get(Tenant, ctx.tenant_id)
    changes = payload.model_dump(exclude_unset=True)
    if changes.get("call_recording_enabled"):
        await plans.ensure_feature(db, ctx.tenant_id, "recording")
    for field, value in changes.items():
        setattr(tenant, field, value)
    await db.commit()
    await db.refresh(tenant)
    return tenant
