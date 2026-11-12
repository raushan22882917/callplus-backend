import uuid
from datetime import UTC, datetime

from app.core.audit import log_audit_event
from app.core.deps import RequestContext, get_db, require_permission
from app.core.permissions import Permissions
from app.core.repository import TenantScopedRepository
from app.schemas.campaign import (
    CampaignAddContacts,
    CampaignContactRead,
    CampaignCreate,
    CampaignEnroll,
    CampaignRead,
    CampaignUpdate,
    EmailDraftRequest,
    EmailDraftResponse,
    SmsDraftRequest,
    SmsDraftResponse,
)
from app.integrations.credentials import get_llm_key
from app.integrations.sarvam import SarvamAPIError
from app.schemas.campaign import PlanRequest, PlanResponse, FromPlanRequest
from app.services import campaign_planner, email_ai, email_compliance, outreach, plans, sms_ai
from app.services.customer_lookup import find_or_create_customer_by_phone
from app.worker.campaign_tasks import run_campaign_task
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import (
    AIAgent,
    Campaign,
    CampaignChannel,
    CampaignContact,
    CampaignStatus,
    Customer,
    PhoneNumber,
    Tenant,
)

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
    await plans.ensure_feature(db, ctx.tenant_id, "campaigns")
    campaign = await CampaignRepository(db, ctx.tenant_id).create(**payload.model_dump())
    await db.commit()
    await db.refresh(campaign)
    return campaign


@router.post("/ai-email", response_model=EmailDraftResponse)
async def draft_email(
    payload: EmailDraftRequest,
    ctx: RequestContext = Depends(require_permission(Permissions.CAMPAIGNS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> EmailDraftResponse:
    """Writes a subject and body. Nothing is saved until the owner uses it."""
    if not payload.instruction.strip() and not payload.current_subject.strip() and not payload.current_body.strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Tell the AI what the email should say.")
    try:
        drafted = await email_ai.draft_email(
            instruction=payload.instruction,
            current_subject=payload.current_subject,
            current_body=payload.current_body,
            api_key=await get_llm_key(db, ctx.tenant_id),
        )
    except SarvamAPIError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "The AI helper is busy. Please try again.") from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    return EmailDraftResponse(**drafted)


@router.post("/ai-sms", response_model=SmsDraftResponse)
async def draft_sms(
    payload: SmsDraftRequest,
    ctx: RequestContext = Depends(require_permission(Permissions.CAMPAIGNS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> SmsDraftResponse:
    if not payload.instruction.strip() and not payload.current_body.strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Tell the AI what the message should say.")
    try:
        body = await sms_ai.draft_sms(
            instruction=payload.instruction, current_body=payload.current_body, api_key=await get_llm_key(db, ctx.tenant_id)
        )
    except SarvamAPIError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "The AI helper is busy. Please try again.") from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    return SmsDraftResponse(body=body)


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


@router.patch("/{campaign_id}", response_model=CampaignRead)
async def update_campaign(
    campaign_id: uuid.UUID,
    payload: CampaignUpdate,
    ctx: RequestContext = Depends(require_permission(Permissions.CAMPAIGNS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> Campaign:
    campaign = await CampaignRepository(db, ctx.tenant_id).get(campaign_id)
    if campaign.status not in (CampaignStatus.DRAFT, CampaignStatus.SCHEDULED):
        raise HTTPException(status.HTTP_409_CONFLICT, "Only a draft or scheduled campaign can be edited.")
    changes = payload.model_dump(exclude_unset=True)
    if campaign.channel == CampaignChannel.EMAIL and ("email_subject" in changes or "email_body" in changes):
        subject = (changes.get("email_subject", campaign.email_subject) or "").strip()
        body = (changes.get("email_body", campaign.email_body) or "").strip()
        if not subject or not body:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "An email campaign needs a subject and a message.")
        changes["email_subject"] = subject
        changes["email_body"] = body
    if campaign.channel == CampaignChannel.SMS and "sms_body" in changes:
        changes["sms_body"] = (changes["sms_body"] or "").strip()
        if not changes["sms_body"]:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "An SMS campaign needs a message.")
    reply_to = changes.get("email_reply_to")
    if isinstance(reply_to, str):
        reply_to = reply_to.strip()
        if reply_to and "@" not in reply_to:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Reply-to must be an email address.")
        changes["email_reply_to"] = reply_to or None
    for key, value in changes.items():
        setattr(campaign, key, value)
    await db.commit()
    await db.refresh(campaign)
    return campaign


@router.post("/{campaign_id}/enroll", response_model=list[CampaignContactRead], status_code=201)
async def enroll_audience(
    campaign_id: uuid.UUID,
    payload: CampaignEnroll,
    ctx: RequestContext = Depends(require_permission(Permissions.CAMPAIGNS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> list[CampaignContact]:
    """Adds every matching customer who is not already on the campaign."""
    campaign = await CampaignRepository(db, ctx.tenant_id).get(campaign_id)
    if campaign.status != CampaignStatus.DRAFT:
        raise HTTPException(status.HTTP_409_CONFLICT, "Add people before the campaign starts.")
    already = set(
        (
            await db.execute(
                select(CampaignContact.customer_id).where(
                    CampaignContact.tenant_id == ctx.tenant_id,
                    CampaignContact.campaign_id == campaign.id,
                )
            )
        ).scalars().all()
    )
    query = select(Customer).where(Customer.tenant_id == ctx.tenant_id)
    if campaign.channel == CampaignChannel.SMS:
        query = query.where(Customer.phone.is_not(None), Customer.phone != "")
    elif payload.audience == "with_email" or campaign.channel == CampaignChannel.EMAIL:
        query = query.where(Customer.email.is_not(None), Customer.email != "")
    elif payload.audience == "with_phone":
        query = query.where(Customer.phone.is_not(None), Customer.phone != "")
    if payload.audience == "tag":
        if not payload.tags:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Choose at least one tag.")
        query = query.where(Customer.tags.overlap(payload.tags))
    customers = list((await db.execute(query.limit(10000))).scalars().all())
    if campaign.channel == CampaignChannel.EMAIL:
        # Unsubscribed, bounced and complaining addresses never join; the send re-checks each one.
        blocked = await email_compliance.suppressed_set(db, ctx.tenant_id, [c.email or "" for c in customers])
        if (await email_compliance.profile(db, ctx.tenant_id)).email_marketing_requires_consent:
            consented = {
                c.id for c in customers if await email_compliance.has_marketing_consent(db, ctx.tenant_id, c.email)
            }
            customers = [c for c in customers if c.id in consented]
        customers = [c for c in customers if email_compliance.normalize_email(c.email) not in blocked]
    if campaign.channel in (CampaignChannel.SMS, CampaignChannel.WHATSAPP):
        # Numbers on the do-not-disturb list (anyone who opted out on any channel) never join.
        customers = [c for c in customers if not await outreach.phone_opted_out(db, ctx.tenant_id, c.phone)]
    contacts = [
        CampaignContact(tenant_id=ctx.tenant_id, campaign_id=campaign.id, customer_id=customer.id)
        for customer in customers
        if customer.id not in already
    ]
    if not contacts:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "No new customers match.")
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


@router.post("/a2p-declaration", status_code=status.HTTP_200_OK)
async def declare_calling_numbers(
    ctx: RequestContext = Depends(require_permission(Permissions.CAMPAIGNS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """The business has told its phone company which numbers place automated calls."""
    tenant = await db.get(Tenant, ctx.tenant_id)
    if tenant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workspace not found")
    tenant.a2p_declared_at = datetime.now(UTC)
    await log_audit_event(
        db,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        action="campaign.a2p_declared",
        resource_type="tenant",
        resource_id=tenant.id,
    )
    await db.commit()
    return {"a2p_declared_at": tenant.a2p_declared_at.isoformat()}


@router.post("/{campaign_id}/launch", response_model=CampaignRead)
async def launch_campaign(
    campaign_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(Permissions.CAMPAIGNS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> Campaign:
    await plans.ensure_feature(db, ctx.tenant_id, "campaigns")
    repo = CampaignRepository(db, ctx.tenant_id)
    campaign = await repo.get(campaign_id)
    if campaign.status != CampaignStatus.DRAFT:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"Campaign is already {campaign.status.value}"
        )
    if campaign.channel == CampaignChannel.VOICE:
        tenant = await db.get(Tenant, ctx.tenant_id)
        if tenant is None or tenant.a2p_declared_at is None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Declare your calling numbers for automated calls before a voice campaign can start.",
            )
    if campaign.channel == CampaignChannel.EMAIL and (
        not (campaign.email_subject or "").strip() or not (campaign.email_body or "").strip()
    ):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "An email campaign needs a subject and a message before it can start.",
        )
    if campaign.channel == CampaignChannel.SMS:
        from app.services.follow_up import _sms_from

        if not (campaign.sms_body or "").strip():
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "An SMS campaign needs a message before it can start.")
        _, why = await _sms_from(db, ctx.tenant_id)
        if why:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, why)
    if campaign.channel == CampaignChannel.EMAIL:
        from app.core.config import get_settings
        from app.services.mailbox import connected_mailbox

        from app.integrations.credentials import get_email_sender

        box = await connected_mailbox(db, ctx.tenant_id)
        settings = get_settings()
        if box is None and await get_email_sender(db, ctx.tenant_id) is None and not settings.email_api_key and not settings.smtp_host:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Connect a mailbox or an email sending key before this campaign can send.",
            )
        if await email_compliance.sender_identity(db, ctx.tenant_id) is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Add your business name and postal address in Settings > Privacy. Marketing email must carry it.",
            )

    scheduled_later = campaign.scheduled_at is not None and campaign.scheduled_at > datetime.now(UTC)
    campaign.status = CampaignStatus.SCHEDULED if scheduled_later else CampaignStatus.RUNNING
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

    if not scheduled_later:  # a scheduled one is started by the minute-by-minute check
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
    if campaign.status not in (CampaignStatus.RUNNING, CampaignStatus.SCHEDULED):
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
    if campaign.channel == CampaignChannel.VOICE:
        tenant = await db.get(Tenant, ctx.tenant_id)
        if tenant is None or tenant.a2p_declared_at is None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Declare your calling numbers for automated calls before a voice campaign can start.",
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


@router.post("/plan", response_model=PlanResponse)
async def plan_campaign(
    payload: PlanRequest,
    ctx: RequestContext = Depends(require_permission(Permissions.CAMPAIGNS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> PlanResponse:
    """Talk the owner through setting up a calling job. Creates nothing."""
    tenant = await db.get(Tenant, ctx.tenant_id)
    count = (
        await db.execute(
            select(func.count()).select_from(Customer).where(
                Customer.tenant_id == ctx.tenant_id, Customer.phone.is_not(None)
            )
        )
    ).scalar_one()
    tag_rows = await db.execute(
        select(func.unnest(Customer.tags)).where(Customer.tenant_id == ctx.tenant_id).distinct().limit(30)
    )
    try:
        result = await campaign_planner.plan_turn(
            messages=[m.model_dump() for m in payload.messages],
            timezone=tenant.timezone if tenant else "Asia/Kolkata",
            tags=[t for (t,) in tag_rows.all()],
            customer_count=count,
            api_key=await get_llm_key(db, ctx.tenant_id),
        )
    except SarvamAPIError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "The AI helper is busy. Please try again.") from exc
    return PlanResponse(**result)


@router.post("/from-plan", response_model=CampaignRead, status_code=status.HTTP_201_CREATED)
async def create_from_plan(
    payload: FromPlanRequest,
    ctx: RequestContext = Depends(require_permission(Permissions.CAMPAIGNS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> Campaign:
    """Creates the campaign the owner approved, adds the people to call, and starts or schedules it."""
    await plans.ensure_feature(db, ctx.tenant_id, "campaigns")
    plan = payload.plan

    agent_id = payload.ai_agent_id
    if agent_id is None:
        # The first AI agent that has a phone number to call from.
        row = await db.execute(
            select(PhoneNumber.ai_agent_id)
            .join(AIAgent, AIAgent.id == PhoneNumber.ai_agent_id)
            .where(PhoneNumber.tenant_id == ctx.tenant_id)
            .limit(1)
        )
        agent_id = row.scalar_one_or_none()
    if agent_id is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Give one of your AI agents a phone number first (Phone numbers), so there is a number to call from.",
        )

    audience = plan.audience
    if audience.type == "numbers":
        customers = []
        for number in audience.numbers:
            customer, _ = await find_or_create_customer_by_phone(db, ctx.tenant_id, number)
            customers.append(customer)
    else:
        query = select(Customer).where(Customer.tenant_id == ctx.tenant_id, Customer.phone.is_not(None))
        if audience.type == "tag":
            query = query.where(Customer.tags.overlap(audience.tags))
        customers = list((await db.execute(query.limit(10000))).scalars().all())
    if not customers:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "No customers match, so there is nobody to call.")

    campaign = await CampaignRepository(db, ctx.tenant_id).create(
        name=plan.name,
        channel=CampaignChannel.VOICE,
        ai_agent_id=agent_id,
        goal=plan.goal,
        questions=plan.questions,
        scheduled_at=plan.scheduled_at,
    )
    for customer in customers:
        db.add(CampaignContact(tenant_id=ctx.tenant_id, campaign_id=campaign.id, customer_id=customer.id))
    await db.commit()
    return await launch_campaign(campaign.id, ctx, db)
