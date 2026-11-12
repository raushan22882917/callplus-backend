"""Email dashboard numbers and saved messages."""

import uuid

from app.core.audit import log_audit_event
from app.core.deps import RequestContext, get_db, require_permission
from app.core.permissions import Permissions
from app.core.repository import TenantScopedRepository
from app.services.email_studio import email_overview
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models.email_template import EmailTemplate

router = APIRouter(tags=["email"])


class DailySend(BaseModel):
    day: str
    count: int


class EmailCampaignStat(BaseModel):
    id: uuid.UUID
    name: str
    status: str
    scheduled_at: str | None
    created_at: str
    sent: int
    failed: int
    pending: int


class EmailOverview(BaseModel):
    campaigns_by_status: dict[str, int]
    contacts_by_status: dict[str, int]
    customers_with_email: int
    customers_total: int
    daily_sends: list[DailySend]
    campaigns: list[EmailCampaignStat]


class EmailTemplateRead(BaseModel):
    id: uuid.UUID
    name: str
    subject: str
    body: str


class EmailTemplateIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=20000)


class EmailTemplateRepository(TenantScopedRepository[EmailTemplate]):
    model = EmailTemplate


def _template(row: EmailTemplate) -> EmailTemplateRead:
    return EmailTemplateRead(id=row.id, name=row.name, subject=row.subject, body=row.body)


@router.get("/email/overview", response_model=EmailOverview)
async def read_email_overview(
    ctx: RequestContext = Depends(require_permission(Permissions.CAMPAIGNS_READ)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    raw = await email_overview(db, ctx.tenant_id)
    raw["daily_sends"] = [{"day": item["day"].isoformat(), "count": item["count"]} for item in raw["daily_sends"]]
    raw["campaigns"] = [
        {
            **item,
            "scheduled_at": item["scheduled_at"].isoformat() if item["scheduled_at"] else None,
            "created_at": item["created_at"].isoformat(),
        }
        for item in raw["campaigns"]
    ]
    return raw


@router.get("/email-templates", response_model=list[EmailTemplateRead])
async def list_email_templates(
    ctx: RequestContext = Depends(require_permission(Permissions.CAMPAIGNS_READ)),
    db: AsyncSession = Depends(get_db),
) -> list[EmailTemplateRead]:
    rows = await EmailTemplateRepository(db, ctx.tenant_id).list(limit=100)
    return [_template(row) for row in rows]


@router.post("/email-templates", response_model=EmailTemplateRead, status_code=status.HTTP_201_CREATED)
async def create_email_template(
    payload: EmailTemplateIn,
    ctx: RequestContext = Depends(require_permission(Permissions.CAMPAIGNS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> EmailTemplateRead:
    name = payload.name.strip()
    taken = (
        await db.execute(
            select(EmailTemplate.id).where(EmailTemplate.tenant_id == ctx.tenant_id, EmailTemplate.name == name)
        )
    ).first()
    if taken:
        raise HTTPException(status.HTTP_409_CONFLICT, "A template with this name already exists.")
    row = await EmailTemplateRepository(db, ctx.tenant_id).create(
        name=name, subject=payload.subject.strip(), body=payload.body.strip()
    )
    await log_audit_event(db, tenant_id=ctx.tenant_id, actor_user_id=ctx.user_id, action="email_template.created", resource_type="email_template", resource_id=row.id)
    await db.commit()
    await db.refresh(row)
    return _template(row)


@router.delete("/email-templates/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_email_template(
    template_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(Permissions.CAMPAIGNS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> None:
    row = await EmailTemplateRepository(db, ctx.tenant_id).get(template_id)
    await db.delete(row)
    await log_audit_event(db, tenant_id=ctx.tenant_id, actor_user_id=ctx.user_id, action="email_template.deleted", resource_type="email_template", resource_id=template_id)
    await db.commit()
