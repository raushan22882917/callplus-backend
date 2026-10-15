import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

from app.core.deps import RequestContext, get_db, require_permission
from app.core.exceptions import NotFoundError
from app.core.permissions import Permissions
from app.core.repository import TenantScopedRepository
from app.schemas.oauth import CrmPushRequest, CrmPushResult
from app.schemas.crm import (
    CompanyCreate,
    CompanyRead,
    CompanyUpdate,
    DealCreate,
    DealRead,
    DealUpdate,
    ImportResult,
    LeadCreate,
    LeadMove,
    LeadRead,
    LeadUpdate,
    NoteCreate,
    NoteRead,
    PipelineRead,
    RescoreResult,
    TaskCreate,
    TaskRead,
    TaskUpdate,
    TimelineItem,
)
from app.services import crm as crm_service
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models.crm import (
    CrmCompany,
    CrmDeal,
    CrmLead,
    CrmNote,
    CrmStage,
    CrmTask,
    CrmTaskStatus,
)

router = APIRouter(prefix="/crm", tags=["crm"])

READ = Permissions.CUSTOMERS_READ
MANAGE = Permissions.CUSTOMERS_MANAGE


class CompanyRepository(TenantScopedRepository[CrmCompany]):
    model = CrmCompany


class LeadRepository(TenantScopedRepository[CrmLead]):
    model = CrmLead


class DealRepository(TenantScopedRepository[CrmDeal]):
    model = CrmDeal


class TaskRepository(TenantScopedRepository[CrmTask]):
    model = CrmTask


class NoteRepository(TenantScopedRepository[CrmNote]):
    model = CrmNote


def _clean_tags(tags: list[str]) -> list[str]:
    seen: list[str] = []
    for t in tags:
        t = t.strip()
        if t and t not in seen:
            seen.append(t)
    return seen


# ------------------------------------------------------------------ pipeline


@router.get("/pipeline", response_model=PipelineRead)
async def get_pipeline(
    per_stage_limit: int = Query(default=50, ge=1, le=200),
    owner_user_id: uuid.UUID | None = Query(default=None),
    q: str | None = Query(default=None, max_length=100),
    ctx: RequestContext = Depends(require_permission(READ)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    stages = await crm_service.pipeline(
        db, ctx.tenant_id, per_stage_limit=per_stage_limit, owner_user_id=owner_user_id, q=q
    )
    return {
        "stages": stages,
        "total_leads": sum(s["count"] for s in stages),
        "total_value": sum((s["total_value"] for s in stages), Decimal(0)),
    }


# ----------------------------------------------------------------- companies


@router.get("/companies", response_model=list[CompanyRead])
async def list_companies(
    q: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=100, le=500),
    offset: int = Query(default=0, ge=0),
    ctx: RequestContext = Depends(require_permission(READ)),
    db: AsyncSession = Depends(get_db),
) -> list[CrmCompany]:
    query = select(CrmCompany).where(CrmCompany.tenant_id == ctx.tenant_id)
    if q:
        query = query.where(CrmCompany.name.ilike(f"%{q.strip()}%"))
    result = await db.execute(query.order_by(CrmCompany.name).limit(limit).offset(offset))
    return list(result.scalars().all())


@router.post("/companies", response_model=CompanyRead, status_code=status.HTTP_201_CREATED)
async def create_company(
    payload: CompanyCreate,
    ctx: RequestContext = Depends(require_permission(MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> CrmCompany:
    company = await CompanyRepository(db, ctx.tenant_id).create(**payload.model_dump())
    await db.commit()
    await db.refresh(company)
    return company


@router.get("/companies/{company_id}", response_model=CompanyRead)
async def get_company(
    company_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(READ)),
    db: AsyncSession = Depends(get_db),
) -> CrmCompany:
    return await CompanyRepository(db, ctx.tenant_id).get(company_id)


@router.patch("/companies/{company_id}", response_model=CompanyRead)
async def update_company(
    company_id: uuid.UUID,
    payload: CompanyUpdate,
    ctx: RequestContext = Depends(require_permission(MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> CrmCompany:
    company = await CompanyRepository(db, ctx.tenant_id).get(company_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        if field == "name" and value is None:
            continue
        setattr(company, field, value)
    await db.commit()
    await db.refresh(company)
    return company


@router.delete("/companies/{company_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_company(
    company_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> None:
    await CompanyRepository(db, ctx.tenant_id).delete(company_id)
    await db.commit()


# --------------------------------------------------------------------- leads


@router.get("/leads", response_model=list[LeadRead])
async def list_leads(
    stage: CrmStage | None = Query(default=None),
    source: str | None = Query(default=None, max_length=80),
    tag: str | None = Query(default=None, max_length=80),
    owner_user_id: uuid.UUID | None = Query(default=None),
    company_id: uuid.UUID | None = Query(default=None),
    customer_id: uuid.UUID | None = Query(default=None),
    min_score: int | None = Query(default=None, ge=0, le=100),
    q: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    ctx: RequestContext = Depends(require_permission(READ)),
    db: AsyncSession = Depends(get_db),
) -> list[CrmLead]:
    query = select(CrmLead).where(CrmLead.tenant_id == ctx.tenant_id)
    if stage:
        query = query.where(CrmLead.stage == stage)
    if source:
        query = query.where(CrmLead.source == source)
    if tag:
        query = query.where(CrmLead.tags.any(tag))
    if owner_user_id:
        query = query.where(CrmLead.owner_user_id == owner_user_id)
    if company_id:
        query = query.where(CrmLead.company_id == company_id)
    if customer_id:
        query = query.where(CrmLead.customer_id == customer_id)
    if min_score is not None:
        query = query.where(CrmLead.score >= min_score)
    if q:
        like = f"%{q.strip()}%"
        query = query.where(
            or_(CrmLead.name.ilike(like), CrmLead.email.ilike(like), CrmLead.phone.ilike(like))
        )
    result = await db.execute(
        query.order_by(CrmLead.created_at.desc()).limit(limit).offset(offset)
    )
    return list(result.scalars().all())


@router.post("/leads", response_model=LeadRead, status_code=status.HTTP_201_CREATED)
async def create_lead(
    payload: LeadCreate,
    ctx: RequestContext = Depends(require_permission(MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> CrmLead:
    await crm_service.ensure_refs(
        db,
        ctx.tenant_id,
        company_id=payload.company_id,
        customer_id=payload.customer_id,
        owner_user_id=payload.owner_user_id,
    )
    data = payload.model_dump()
    manual_score = data.pop("score")
    data["tags"] = _clean_tags(data["tags"])
    lead = await LeadRepository(db, ctx.tenant_id).create(**data)
    await crm_service.rescore_lead(db, lead)
    if manual_score is not None:
        lead.score = manual_score
        lead.score_breakdown = [{"rule": "manual", "points": manual_score, "detail": "Set manually"}]
    await crm_service.push_lead_to_connected_crms(db, lead)
    await db.commit()
    await db.refresh(lead)
    return lead


@router.post("/leads/{lead_id}/push-to-crm", response_model=list[CrmPushResult])
async def push_lead_to_crm(
    lead_id: uuid.UUID,
    payload: CrmPushRequest | None = None,
    ctx: RequestContext = Depends(require_permission(MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """Manually push this lead to a connected HubSpot / Zoho / Salesforce
    (all connected ones unless `provider` is given)."""
    from app.integrations.oauth.errors import OAuthError
    from app.services import crm_push

    lead = await LeadRepository(db, ctx.tenant_id).get(lead_id)
    try:
        results = await crm_push.push_lead(
            db, lead, provider=payload.provider if payload else None
        )
    except OAuthError as exc:
        raise HTTPException(exc.status_code, detail=exc.as_detail()) from exc
    if not results:
        raise HTTPException(
            400,
            detail={"code": "no_crm_connected", "message": "Connect HubSpot, Zoho or Salesforce first"},
        )
    await db.commit()
    return results


@router.post("/leads/import", response_model=ImportResult)
async def import_leads(
    file: UploadFile = File(...),
    source: str = Query(default="import", max_length=80),
    ctx: RequestContext = Depends(require_permission(MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    raw = await file.read(crm_service.MAX_IMPORT_BYTES + 1)
    if len(raw) > crm_service.MAX_IMPORT_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "CSV must be under 2 MB")
    try:
        created, skipped = await crm_service.import_leads_csv(
            db, ctx.tenant_id, raw, default_source=source
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    await db.commit()
    return {"created": created, "skipped": skipped}


@router.post("/leads/rescore", response_model=RescoreResult)
async def rescore_all_leads(
    ctx: RequestContext = Depends(require_permission(MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    count = await crm_service.rescore_all(db, ctx.tenant_id)
    await db.commit()
    return {"rescored": count}


@router.get("/leads/{lead_id}", response_model=LeadRead)
async def get_lead(
    lead_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(READ)),
    db: AsyncSession = Depends(get_db),
) -> CrmLead:
    return await LeadRepository(db, ctx.tenant_id).get(lead_id)


@router.patch("/leads/{lead_id}", response_model=LeadRead)
async def update_lead(
    lead_id: uuid.UUID,
    payload: LeadUpdate,
    ctx: RequestContext = Depends(require_permission(MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> CrmLead:
    lead = await LeadRepository(db, ctx.tenant_id).get(lead_id)
    data = payload.model_dump(exclude_unset=True)
    await crm_service.ensure_refs(
        db,
        ctx.tenant_id,
        company_id=data.get("company_id"),
        customer_id=data.get("customer_id"),
        owner_user_id=data.get("owner_user_id"),
    )
    manual_score = data.pop("score", None)
    for field in ("name", "tags", "custom_fields", "value", "currency"):
        if field in data and data[field] is None:
            data.pop(field)
    if "tags" in data:
        data["tags"] = _clean_tags(data["tags"])
    for field, value in data.items():
        setattr(lead, field, value)
    if manual_score is not None:
        lead.score = manual_score
        lead.score_breakdown = [{"rule": "manual", "points": manual_score, "detail": "Set manually"}]
    await db.commit()
    await db.refresh(lead)
    return lead


@router.delete("/leads/{lead_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_lead(
    lead_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> None:
    await LeadRepository(db, ctx.tenant_id).delete(lead_id)
    await db.commit()


@router.post("/leads/{lead_id}/move", response_model=LeadRead)
async def move_lead(
    lead_id: uuid.UUID,
    payload: LeadMove,
    ctx: RequestContext = Depends(require_permission(MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> CrmLead:
    lead = await LeadRepository(db, ctx.tenant_id).get(lead_id)
    await crm_service.move_lead(
        db, lead, payload.stage, lost_reason=payload.lost_reason, author_user_id=ctx.user_id
    )
    await db.commit()
    await db.refresh(lead)
    return lead


@router.post("/leads/{lead_id}/rescore", response_model=LeadRead)
async def rescore_lead(
    lead_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> CrmLead:
    lead = await LeadRepository(db, ctx.tenant_id).get(lead_id)
    await crm_service.rescore_lead(db, lead)
    await db.commit()
    await db.refresh(lead)
    return lead


@router.get("/leads/{lead_id}/timeline", response_model=list[TimelineItem])
async def lead_timeline(
    lead_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(READ)),
    db: AsyncSession = Depends(get_db),
) -> list[TimelineItem]:
    lead = await LeadRepository(db, ctx.tenant_id).get(lead_id)
    items: list[TimelineItem] = []

    notes = await db.execute(
        select(CrmNote).where(CrmNote.tenant_id == ctx.tenant_id, CrmNote.lead_id == lead.id)
    )
    for n in notes.scalars().all():
        is_stage = n.kind == "stage_change"
        items.append(
            TimelineItem(
                type="stage_change" if is_stage else "note",
                id=n.id,
                at=n.created_at,
                title="Stage changed" if is_stage else "Note",
                body=n.body,
                data={"author_user_id": str(n.author_user_id) if n.author_user_id else None},
            )
        )
    tasks = await db.execute(
        select(CrmTask).where(CrmTask.tenant_id == ctx.tenant_id, CrmTask.lead_id == lead.id)
    )
    for t in tasks.scalars().all():
        items.append(
            TimelineItem(
                type="task",
                id=t.id,
                at=t.created_at,
                title=t.title,
                body=t.description,
                data={
                    "task_type": t.type.value,
                    "status": t.status.value,
                    "due_at": t.due_at.isoformat() if t.due_at else None,
                },
            )
        )
    for c in await crm_service.calls_for_lead(db, lead):
        items.append(
            TimelineItem(
                type="call",
                id=c.id,
                at=c.started_at or c.created_at,
                title=f"{c.direction.value.capitalize()} call",
                body=c.summary,
                data={
                    "status": c.status.value,
                    "sentiment": c.sentiment,
                    "outcome": c.outcome,
                    "intent": c.intent,
                    "duration_seconds": c.duration_seconds,
                },
            )
        )
    items.sort(key=lambda i: i.at, reverse=True)
    return items


# --------------------------------------------------------------------- deals


@router.get("/deals", response_model=list[DealRead])
async def list_deals(
    stage: CrmStage | None = Query(default=None),
    lead_id: uuid.UUID | None = Query(default=None),
    owner_user_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=100, le=500),
    offset: int = Query(default=0, ge=0),
    ctx: RequestContext = Depends(require_permission(READ)),
    db: AsyncSession = Depends(get_db),
) -> list[CrmDeal]:
    query = select(CrmDeal).where(CrmDeal.tenant_id == ctx.tenant_id)
    if stage:
        query = query.where(CrmDeal.stage == stage)
    if lead_id:
        query = query.where(CrmDeal.lead_id == lead_id)
    if owner_user_id:
        query = query.where(CrmDeal.owner_user_id == owner_user_id)
    result = await db.execute(
        query.order_by(CrmDeal.created_at.desc()).limit(limit).offset(offset)
    )
    return list(result.scalars().all())


def _apply_deal_close(deal: CrmDeal) -> None:
    if deal.stage in (CrmStage.WON, CrmStage.LOST):
        deal.closed_at = deal.closed_at or datetime.now(UTC)
    else:
        deal.closed_at = None


@router.post("/deals", response_model=DealRead, status_code=status.HTTP_201_CREATED)
async def create_deal(
    payload: DealCreate,
    ctx: RequestContext = Depends(require_permission(MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> CrmDeal:
    await crm_service.ensure_refs(
        db,
        ctx.tenant_id,
        company_id=payload.company_id,
        customer_id=payload.customer_id,
        owner_user_id=payload.owner_user_id,
        lead_id=payload.lead_id,
    )
    deal = await DealRepository(db, ctx.tenant_id).create(**payload.model_dump())
    _apply_deal_close(deal)
    await db.commit()
    await db.refresh(deal)
    return deal


@router.get("/deals/{deal_id}", response_model=DealRead)
async def get_deal(
    deal_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(READ)),
    db: AsyncSession = Depends(get_db),
) -> CrmDeal:
    return await DealRepository(db, ctx.tenant_id).get(deal_id)


@router.patch("/deals/{deal_id}", response_model=DealRead)
async def update_deal(
    deal_id: uuid.UUID,
    payload: DealUpdate,
    ctx: RequestContext = Depends(require_permission(MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> CrmDeal:
    deal = await DealRepository(db, ctx.tenant_id).get(deal_id)
    data = payload.model_dump(exclude_unset=True)
    await crm_service.ensure_refs(
        db,
        ctx.tenant_id,
        company_id=data.get("company_id"),
        customer_id=data.get("customer_id"),
        owner_user_id=data.get("owner_user_id"),
        lead_id=data.get("lead_id"),
    )
    for field in ("title", "stage", "value", "currency"):
        if field in data and data[field] is None:
            data.pop(field)
    for field, value in data.items():
        setattr(deal, field, value)
    _apply_deal_close(deal)
    await db.commit()
    await db.refresh(deal)
    return deal


@router.delete("/deals/{deal_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_deal(
    deal_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> None:
    await DealRepository(db, ctx.tenant_id).delete(deal_id)
    await db.commit()


# --------------------------------------------------------------------- tasks


@router.get("/tasks", response_model=list[TaskRead])
async def list_tasks(
    due: Literal["today", "overdue"] | None = Query(default=None),
    task_status: CrmTaskStatus | None = Query(default=None, alias="status"),
    assignee_user_id: uuid.UUID | None = Query(default=None),
    lead_id: uuid.UUID | None = Query(default=None),
    deal_id: uuid.UUID | None = Query(default=None),
    customer_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=100, le=500),
    offset: int = Query(default=0, ge=0),
    ctx: RequestContext = Depends(require_permission(READ)),
    db: AsyncSession = Depends(get_db),
) -> list[CrmTask]:
    """`due=today` is the current UTC day, `due=overdue` is before now; both
    only return open tasks."""
    query = select(CrmTask).where(CrmTask.tenant_id == ctx.tenant_id)
    now = datetime.now(UTC)
    if due == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        query = query.where(
            CrmTask.status == CrmTaskStatus.OPEN,
            CrmTask.due_at >= start,
            CrmTask.due_at < start + timedelta(days=1),
        )
    elif due == "overdue":
        query = query.where(CrmTask.status == CrmTaskStatus.OPEN, CrmTask.due_at < now)
    if task_status:
        query = query.where(CrmTask.status == task_status)
    if assignee_user_id:
        query = query.where(CrmTask.assignee_user_id == assignee_user_id)
    if lead_id:
        query = query.where(CrmTask.lead_id == lead_id)
    if deal_id:
        query = query.where(CrmTask.deal_id == deal_id)
    if customer_id:
        query = query.where(CrmTask.customer_id == customer_id)
    result = await db.execute(
        query.order_by(CrmTask.due_at.asc().nulls_last(), CrmTask.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars().all())


@router.post("/tasks", response_model=TaskRead, status_code=status.HTTP_201_CREATED)
async def create_task(
    payload: TaskCreate,
    ctx: RequestContext = Depends(require_permission(MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> CrmTask:
    await crm_service.ensure_refs(
        db,
        ctx.tenant_id,
        lead_id=payload.lead_id,
        deal_id=payload.deal_id,
        customer_id=payload.customer_id,
        owner_user_id=payload.assignee_user_id,
    )
    task = await TaskRepository(db, ctx.tenant_id).create(**payload.model_dump())
    await db.commit()
    await db.refresh(task)
    return task


@router.get("/tasks/{task_id}", response_model=TaskRead)
async def get_task(
    task_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(READ)),
    db: AsyncSession = Depends(get_db),
) -> CrmTask:
    return await TaskRepository(db, ctx.tenant_id).get(task_id)


@router.patch("/tasks/{task_id}", response_model=TaskRead)
async def update_task(
    task_id: uuid.UUID,
    payload: TaskUpdate,
    ctx: RequestContext = Depends(require_permission(MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> CrmTask:
    task = await TaskRepository(db, ctx.tenant_id).get(task_id)
    data = payload.model_dump(exclude_unset=True)
    await crm_service.ensure_refs(
        db, ctx.tenant_id, owner_user_id=data.get("assignee_user_id")
    )
    for field in ("title", "type", "status"):
        if field in data and data[field] is None:
            data.pop(field)
    for field, value in data.items():
        setattr(task, field, value)
    if "status" in data:
        task.completed_at = datetime.now(UTC) if task.status == CrmTaskStatus.DONE else None
    await db.commit()
    await db.refresh(task)
    return task


@router.delete("/tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_task(
    task_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> None:
    await TaskRepository(db, ctx.tenant_id).delete(task_id)
    await db.commit()


# --------------------------------------------------------------------- notes


@router.get("/notes", response_model=list[NoteRead])
async def list_notes(
    lead_id: uuid.UUID | None = Query(default=None),
    deal_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=100, le=500),
    ctx: RequestContext = Depends(require_permission(READ)),
    db: AsyncSession = Depends(get_db),
) -> list[CrmNote]:
    query = select(CrmNote).where(CrmNote.tenant_id == ctx.tenant_id)
    if lead_id:
        query = query.where(CrmNote.lead_id == lead_id)
    if deal_id:
        query = query.where(CrmNote.deal_id == deal_id)
    result = await db.execute(query.order_by(CrmNote.created_at.desc()).limit(limit))
    return list(result.scalars().all())


@router.post("/notes", response_model=NoteRead, status_code=status.HTTP_201_CREATED)
async def create_note(
    payload: NoteCreate,
    ctx: RequestContext = Depends(require_permission(MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> CrmNote:
    if payload.lead_id is None and payload.deal_id is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "lead_id or deal_id is required")
    await crm_service.ensure_refs(
        db, ctx.tenant_id, lead_id=payload.lead_id, deal_id=payload.deal_id
    )
    note = await NoteRepository(db, ctx.tenant_id).create(
        **payload.model_dump(), author_user_id=ctx.user_id
    )
    if payload.lead_id:
        lead = await LeadRepository(db, ctx.tenant_id).get(payload.lead_id)
        await crm_service.rescore_lead(db, lead)
    await db.commit()
    await db.refresh(note)
    return note


@router.delete("/notes/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_note(
    note_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> None:
    repo = NoteRepository(db, ctx.tenant_id)
    note = await repo.get(note_id)
    if note.kind != "note":
        raise NotFoundError("Note not found")
    await repo.delete(note_id)
    await db.commit()
