import uuid

from app.core.deps import RequestContext, get_db, require_permission
from app.core.permissions import Permissions
from app.schemas.csat import CsatSettingsRead, CsatSettingsUpdate, CsatSurveyRead
from app.services import csat as svc
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models.csat import CsatSettings, CsatSurvey

router = APIRouter(prefix="/csat", tags=["csat"])


@router.get("/settings", response_model=CsatSettingsRead)
async def get_csat_settings(
    ctx: RequestContext = Depends(require_permission(Permissions.ANALYTICS_READ)),
    db: AsyncSession = Depends(get_db),
) -> CsatSettings:
    row = await svc.get_settings_row(db, ctx.tenant_id)
    await db.commit()
    return row


@router.patch("/settings", response_model=CsatSettingsRead)
async def update_csat_settings(
    payload: CsatSettingsUpdate,
    ctx: RequestContext = Depends(require_permission(Permissions.SETTINGS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> CsatSettings:
    row = await svc.get_settings_row(db, ctx.tenant_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(row, field, value)
    await db.commit()
    await db.refresh(row)
    return row


@router.get("/summary")
async def csat_summary(
    days: int = Query(default=30, ge=1, le=365),
    ctx: RequestContext = Depends(require_permission(Permissions.ANALYTICS_READ)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await svc.summary(db, ctx.tenant_id, days=days)


@router.get("/surveys", response_model=list[CsatSurveyRead])
async def list_csat_surveys(
    status: str | None = Query(default=None, max_length=10),
    customer_id: uuid.UUID | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    ctx: RequestContext = Depends(require_permission(Permissions.ANALYTICS_READ)),
    db: AsyncSession = Depends(get_db),
) -> list[CsatSurvey]:
    query = select(CsatSurvey).where(CsatSurvey.tenant_id == ctx.tenant_id)
    if status:
        query = query.where(CsatSurvey.status == status)
    if customer_id:
        query = query.where(CsatSurvey.customer_id == customer_id)
    rows = await db.execute(
        query.order_by(CsatSurvey.created_at.desc()).limit(limit).offset(offset)
    )
    return list(rows.scalars().all())
