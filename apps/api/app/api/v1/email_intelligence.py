"""AI help around an email campaign: when to send, whether it will land, better subject lines, how it did,
and the weekly autopilot."""

import uuid
from datetime import UTC, datetime
from typing import Literal

from app.core.audit import log_audit_event
from app.core.deps import RequestContext, get_db, require_permission
from app.core.permissions import Permissions
from app.integrations.credentials import get_llm_key
from app.integrations.sarvam import SarvamAPIError
from app.services import email_ai, email_autopilot, email_checker, email_insights, plans
from app.services.email_studio import email_overview
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import Campaign, CampaignChannel, CampaignContact, CampaignContactStatus, CampaignStatus, Tenant

router = APIRouter(tags=["email-ai"])
Read = Depends(require_permission(Permissions.CAMPAIGNS_READ))
Manage = Depends(require_permission(Permissions.CAMPAIGNS_MANAGE))


class Slot(BaseModel):
    at: str
    local: str
    score: int
    reason: str
    source: str


class SendTimes(BaseModel):
    timezone: str
    slots: list[Slot]
    heatmap: list[list[int]]
    events: int
    learned: float


class CheckIn(BaseModel):
    subject: str = Field(default="", max_length=200)
    body: str = Field(default="", max_length=20000)


class Issue(BaseModel):
    level: str
    field: str
    text: str


class CheckOut(BaseModel):
    score: int
    label: str
    issues: list[Issue]
    stats: dict[str, int]


class SubjectVariant(BaseModel):
    subject: str
    angle: str
    score: int
    label: str


class SubjectsOut(BaseModel):
    variants: list[SubjectVariant]


@router.get("/email/ai/send-times", response_model=SendTimes)
async def send_times(ctx: RequestContext = Read, db: AsyncSession = Depends(get_db)) -> dict:
    return await email_insights.send_time_plan(db, ctx.tenant_id)


@router.post("/email/ai/check", response_model=CheckOut)
async def check(payload: CheckIn, ctx: RequestContext = Read) -> dict:
    return email_checker.check_email(payload.subject, payload.body)


@router.post("/email/ai/subjects", response_model=SubjectsOut)
async def subjects(payload: CheckIn, ctx: RequestContext = Manage, db: AsyncSession = Depends(get_db)) -> dict:
    if not payload.body.strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Write the message first, then the AI can suggest subject lines.")
    try:
        found = await email_ai.suggest_subjects(subject=payload.subject, body=payload.body, api_key=await get_llm_key(db, ctx.tenant_id))
    except SarvamAPIError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "The AI helper is busy. Please try again.") from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    out = []
    for item in found:
        verdict = email_checker.check_email(item["subject"], payload.body)
        out.append({**item, "score": verdict["score"], "label": verdict["label"]})
    out.sort(key=lambda v: -v["score"])
    return {"variants": out}


# ---- one campaign's report ---------------------------------------------------------------------


class LinkRow(BaseModel):
    url: str
    clicks: int
    people: int


class CampaignReport(BaseModel):
    id: uuid.UUID
    name: str
    status: str
    sent: int
    failed: int
    pending: int
    opened: int
    clicked: int
    open_rate: float
    click_rate: float
    links: list[LinkRow]
    note: str


@router.get("/email/campaigns/{campaign_id}/report", response_model=CampaignReport)
async def campaign_report(campaign_id: uuid.UUID, ctx: RequestContext = Read, db: AsyncSession = Depends(get_db)) -> dict:
    campaign = await db.get(Campaign, campaign_id)
    if campaign is None or campaign.tenant_id != ctx.tenant_id or campaign.channel != CampaignChannel.EMAIL:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Campaign not found")
    rows = (
        await db.execute(
            select(CampaignContact.status, func.count())
            .where(CampaignContact.tenant_id == ctx.tenant_id, CampaignContact.campaign_id == campaign_id)
            .group_by(CampaignContact.status)
        )
    ).all()
    counts = {(s.value if hasattr(s, "value") else str(s)): int(n) for s, n in rows}
    sent = counts.get("sent", 0) + counts.get("completed", 0)
    engagement = (await email_insights.engagement_by_campaign(db, ctx.tenant_id, [campaign_id])).get(campaign_id, {})
    opened, clicked = engagement.get("opened", 0), engagement.get("clicked", 0)
    return {
        "id": campaign.id,
        "name": campaign.name,
        "status": campaign.status.value,
        "sent": sent,
        "failed": counts.get("failed", 0),
        "pending": counts.get("pending", 0),
        "opened": opened,
        "clicked": clicked,
        "open_rate": round(100 * opened / sent, 1) if sent else 0.0,
        "click_rate": round(100 * clicked / sent, 1) if sent else 0.0,
        "links": await email_insights.top_links(db, ctx.tenant_id, campaign_id),
        "note": "Opens are an estimate: some mail apps load images in advance and some block them. Clicks are exact.",
    }


# ---- schedule a draft at the best time ----------------------------------------------------------


class AutoScheduleIn(BaseModel):
    at: datetime | None = None  # a slot from send-times; the best one when omitted
    launch: bool = False


class AutoScheduleOut(BaseModel):
    scheduled_at: str
    local: str
    status: str
    reason: str


@router.post("/campaigns/{campaign_id}/auto-schedule", response_model=AutoScheduleOut)
async def auto_schedule(campaign_id: uuid.UUID, payload: AutoScheduleIn, ctx: RequestContext = Manage, db: AsyncSession = Depends(get_db)) -> dict:
    campaign = await db.get(Campaign, campaign_id)
    if campaign is None or campaign.tenant_id != ctx.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Campaign not found")
    if campaign.channel != CampaignChannel.EMAIL:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Smart scheduling is for email campaigns.")
    if campaign.status not in (CampaignStatus.DRAFT, CampaignStatus.SCHEDULED):
        raise HTTPException(status.HTTP_409_CONFLICT, f"Campaign is already {campaign.status.value}")
    plan = await email_insights.send_time_plan(db, ctx.tenant_id)
    slots = plan["slots"]
    if payload.at is not None:
        wanted = payload.at if payload.at.tzinfo else payload.at.replace(tzinfo=UTC)
        slot = next((s for s in slots if datetime.fromisoformat(s["at"]) == wanted), None)
        if slot is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "That time is not one of the suggested times. Ask for the times again.")
    elif slots:
        slot = slots[0]
    else:
        raise HTTPException(status.HTTP_409_CONFLICT, "No good time was found in the next week.")
    campaign.scheduled_at = datetime.fromisoformat(slot["at"])
    await log_audit_event(db, tenant_id=ctx.tenant_id, actor_user_id=ctx.user_id, action="campaign.auto_scheduled", resource_type="campaign", resource_id=campaign.id)
    await db.commit()
    if payload.launch and campaign.status == CampaignStatus.DRAFT:
        from app.api.v1.campaigns import launch_campaign

        await launch_campaign(campaign_id, ctx, db)
        await db.refresh(campaign)
    return {"scheduled_at": slot["at"], "local": slot["local"], "status": campaign.status.value, "reason": slot["reason"]}


# ---- weekly autopilot --------------------------------------------------------------------------


class AutopilotIn(BaseModel):
    enabled: bool
    goal: str = Field(default="", max_length=1000)
    mode: Literal["review", "send"] = "review"
    day: int = Field(default=0, ge=0, le=6)


class AutopilotOut(BaseModel):
    enabled: bool
    goal: str
    mode: str
    day: int
    last_week: str | None
    last_campaign_id: str | None
    last_run_note: str | None
    ready: str | None  # why it cannot send yet, if so


class AutopilotRun(BaseModel):
    status: str
    reason: str | None = None
    campaign_id: str | None = None
    score: int | None = None


async def _autopilot_out(db: AsyncSession, tenant: Tenant) -> dict:
    return {**email_autopilot.config(tenant), "ready": await email_autopilot.not_ready_reason(db, tenant.id)}


@router.get("/email/autopilot", response_model=AutopilotOut)
async def get_autopilot(ctx: RequestContext = Read, db: AsyncSession = Depends(get_db)) -> dict:
    return await _autopilot_out(db, await db.get(Tenant, ctx.tenant_id))


@router.put("/email/autopilot", response_model=AutopilotOut)
async def put_autopilot(payload: AutopilotIn, ctx: RequestContext = Manage, db: AsyncSession = Depends(get_db)) -> dict:
    if payload.enabled and not payload.goal.strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Tell the autopilot what to send about, for example: a weekly tip and a reminder to book.")
    await plans.ensure_feature(db, ctx.tenant_id, "campaigns")
    tenant = await db.get(Tenant, ctx.tenant_id)
    email_autopilot.save(tenant, enabled=payload.enabled, goal=payload.goal, mode=payload.mode, day=payload.day)
    await log_audit_event(db, tenant_id=ctx.tenant_id, actor_user_id=ctx.user_id, action="email.autopilot_saved", resource_type="tenant", resource_id=tenant.id)
    await db.commit()
    return await _autopilot_out(db, tenant)


@router.post("/email/autopilot/run", response_model=AutopilotRun)
async def run_autopilot(ctx: RequestContext = Manage, db: AsyncSession = Depends(get_db)) -> dict:
    """Draft this week's email now instead of waiting for the day. It is always a draft for the owner to review."""
    await plans.ensure_feature(db, ctx.tenant_id, "campaigns")
    tenant = await db.get(Tenant, ctx.tenant_id)
    original = dict(tenant.email_autopilot or {})
    tenant.email_autopilot = {**original, "mode": "review"}  # a manual run never schedules by itself
    result = await email_autopilot.run_for_tenant(db, tenant, force=True)
    state = dict(tenant.email_autopilot or {})
    state["mode"] = original.get("mode", "review")
    tenant.email_autopilot = state
    await db.commit()
    return result
