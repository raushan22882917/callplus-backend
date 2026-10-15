import uuid

from app.core.audit import log_audit_event
from app.core.deps import RequestContext, get_db, require_permission
from app.core.permissions import Permissions
from app.core.repository import TenantScopedRepository
from app.schemas.campaign import (
    CampaignAddContacts,
    CampaignContactRead,
    CampaignCreate,
    CampaignRead,
)
from app.worker.campaign_tasks import run_campaign_task
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import Campaign, CampaignContact, CampaignStatus

router = APIRouter(prefix="/campaigns", tags=["campaigns"])


class CampaignRepository(TenantScopedRepository[Campaign]):
    model = Campaign


class CampaignContactRepository(TenantScopedRepository[CampaignContact]):
    model = CampaignContact


@router.get("", response_model=list[CampaignRead])
async def list_campaigns(
    ctx: RequestContext = Depends(require_permission(Permissions.CAMPAIGNS_READ)),
    db: AsyncSession = Depends(get_db),
) -> list[Campaign]:
    return await CampaignRepository(db, ctx.tenant_id).list()


@router.post("", response_model=CampaignRead, status_code=status.HTTP_201_CREATED)
async def create_campaign(
    payload: CampaignCreate,
    ctx: RequestContext = Depends(require_permission(Permissions.CAMPAIGNS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> Campaign:
    campaign = await CampaignRepository(db, ctx.tenant_id).create(**payload.model_dump())
    await db.commit()
    await db.refresh(campaign)
    return campaign


@router.get("/{campaign_id}", response_model=CampaignRead)
async def get_campaign(
    campaign_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(Permissions.CAMPAIGNS_READ)),
    db: AsyncSession = Depends(get_db),
) -> Campaign:
    return await CampaignRepository(db, ctx.tenant_id).get(campaign_id)


@router.post("/{campaign_id}/contacts", response_model=list[CampaignContactRead], status_code=201)
async def add_campaign_contacts(
    campaign_id: uuid.UUID,
    payload: CampaignAddContacts,
    ctx: RequestContext = Depends(require_permission(Permissions.CAMPAIGNS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> list[CampaignContact]:
    await CampaignRepository(db, ctx.tenant_id).get(campaign_id)  # 404s if not in this tenant

    contacts = [
        CampaignContact(tenant_id=ctx.tenant_id, campaign_id=campaign_id, customer_id=customer_id)
        for customer_id in payload.customer_ids
    ]
    db.add_all(contacts)
    await db.commit()
    for contact in contacts:
        await db.refresh(contact)
    return contacts


@router.get("/{campaign_id}/contacts", response_model=list[CampaignContactRead])
async def list_campaign_contacts(
    campaign_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(Permissions.CAMPAIGNS_READ)),
    db: AsyncSession = Depends(get_db),
) -> list[CampaignContact]:
    await CampaignRepository(db, ctx.tenant_id).get(campaign_id)  # 404s if not in this tenant
    result = await db.execute(
        select(CampaignContact).where(
            CampaignContact.tenant_id == ctx.tenant_id, CampaignContact.campaign_id == campaign_id
        )
    )
    return list(result.scalars().all())


@router.post("/{campaign_id}/launch", response_model=CampaignRead)
async def launch_campaign(
    campaign_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(Permissions.CAMPAIGNS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> Campaign:
    repo = CampaignRepository(db, ctx.tenant_id)
    campaign = await repo.get(campaign_id)
    if campaign.status != CampaignStatus.DRAFT:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"Campaign is already {campaign.status.value}"
        )

    campaign.status = CampaignStatus.RUNNING
    await log_audit_event(
        db,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        action="campaign.launched",
        resource_type="campaign",
        resource_id=campaign.id,
    )
    await db.commit()
    await db.refresh(campaign)

    run_campaign_task.delay(str(campaign.id))
    return campaign


@router.post("/{campaign_id}/pause", response_model=CampaignRead)
async def pause_campaign(
    campaign_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(Permissions.CAMPAIGNS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> Campaign:
    repo = CampaignRepository(db, ctx.tenant_id)
    campaign = await repo.get(campaign_id)
    if campaign.status != CampaignStatus.RUNNING:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"Campaign is {campaign.status.value}, not running"
        )

    campaign.status = CampaignStatus.PAUSED
    await log_audit_event(
        db,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        action="campaign.paused",
        resource_type="campaign",
        resource_id=campaign.id,
    )
    await db.commit()
    await db.refresh(campaign)
    return campaign


@router.post("/{campaign_id}/resume", response_model=CampaignRead)
async def resume_campaign(
    campaign_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(Permissions.CAMPAIGNS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> Campaign:
    repo = CampaignRepository(db, ctx.tenant_id)
    campaign = await repo.get(campaign_id)
    if campaign.status != CampaignStatus.PAUSED:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"Campaign is {campaign.status.value}, not paused"
        )

    campaign.status = CampaignStatus.RUNNING
    await log_audit_event(
        db,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        action="campaign.resumed",
        resource_type="campaign",
        resource_id=campaign.id,
    )
    await db.commit()
    await db.refresh(campaign)

    run_campaign_task.delay(str(campaign.id))
    return campaign
