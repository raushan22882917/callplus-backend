import uuid

from app.core.audit import log_audit_event
from app.core.deps import RequestContext, get_db, require_permission
from app.core.exceptions import NotFoundError
from app.core.permissions import Permissions
from app.core.repository import TenantScopedRepository
from app.schemas.guardrails import (
    GuardrailEventRead,
    GuardrailRuleCreate,
    GuardrailRuleRead,
    GuardrailRuleUpdate,
    GuardrailTemplate,
    KnowledgeControlRead,
    KnowledgeControlUpdate,
    validate_rule,
)
from app.services.guardrails import DEFAULT_TEMPLATES
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import AIAgent, KnowledgeDocument
from packages.database.models.guardrails import GuardrailEvent, GuardrailRule, KnowledgeAIControl

router = APIRouter(prefix="/guardrails", tags=["guardrails"])


class GuardrailRuleRepository(TenantScopedRepository[GuardrailRule]):
    model = GuardrailRule


async def _check_agent(db: AsyncSession, tenant_id: uuid.UUID, agent_id: uuid.UUID | None) -> None:
    if agent_id is None:
        return
    found = await db.execute(select(AIAgent.id).where(AIAgent.id == agent_id, AIAgent.tenant_id == tenant_id))
    if found.scalar_one_or_none() is None:
        raise NotFoundError("AIAgent not found")


@router.get("/defaults", response_model=list[GuardrailTemplate])
async def list_default_rules(
    ctx: RequestContext = Depends(require_permission(Permissions.AGENTS_READ)),
) -> list[dict]:
    return DEFAULT_TEMPLATES


@router.get("/rules", response_model=list[GuardrailRuleRead])
async def list_rules(
    ai_agent_id: uuid.UUID | None = None,
    limit: int = Query(default=200, le=500),
    offset: int = Query(default=0, ge=0),
    ctx: RequestContext = Depends(require_permission(Permissions.AGENTS_READ)),
    db: AsyncSession = Depends(get_db),
) -> list[GuardrailRule]:
    query = select(GuardrailRule).where(GuardrailRule.tenant_id == ctx.tenant_id)
    if ai_agent_id is not None:
        query = query.where(GuardrailRule.ai_agent_id == ai_agent_id)
    result = await db.execute(
        query.order_by(GuardrailRule.priority, GuardrailRule.created_at).limit(limit).offset(offset)
    )
    return list(result.scalars().all())


@router.post("/rules", response_model=GuardrailRuleRead, status_code=status.HTTP_201_CREATED)
async def create_rule(
    payload: GuardrailRuleCreate,
    ctx: RequestContext = Depends(require_permission(Permissions.AGENTS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> GuardrailRule:
    await _check_agent(db, ctx.tenant_id, payload.ai_agent_id)
    rule = await GuardrailRuleRepository(db, ctx.tenant_id).create(**payload.model_dump())
    await log_audit_event(
        db, tenant_id=ctx.tenant_id, actor_user_id=ctx.user_id, action="guardrail_rule.created",
        resource_type="guardrail_rule", resource_id=rule.id, extra={"kind": rule.kind},
    )
    await db.commit()
    await db.refresh(rule)
    return rule


@router.get("/rules/{rule_id}", response_model=GuardrailRuleRead)
async def get_rule(
    rule_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(Permissions.AGENTS_READ)),
    db: AsyncSession = Depends(get_db),
) -> GuardrailRule:
    return await GuardrailRuleRepository(db, ctx.tenant_id).get(rule_id)


@router.patch("/rules/{rule_id}", response_model=GuardrailRuleRead)
async def update_rule(
    rule_id: uuid.UUID,
    payload: GuardrailRuleUpdate,
    ctx: RequestContext = Depends(require_permission(Permissions.AGENTS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> GuardrailRule:
    rule = await GuardrailRuleRepository(db, ctx.tenant_id).get(rule_id)
    changes = payload.model_dump(exclude_unset=True)
    if changes.get("kind") is None:
        changes.pop("kind", None)
    if changes.get("params") is None:
        changes.pop("params", None)
    if "ai_agent_id" in changes:
        await _check_agent(db, ctx.tenant_id, changes["ai_agent_id"])
    try:
        validate_rule(
            changes.get("kind", rule.kind),
            changes["text"] if "text" in changes else rule.text,
            changes.get("params", rule.params),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    for field, value in changes.items():
        setattr(rule, field, value)
    await log_audit_event(
        db, tenant_id=ctx.tenant_id, actor_user_id=ctx.user_id, action="guardrail_rule.updated",
        resource_type="guardrail_rule", resource_id=rule.id, extra={"fields": sorted(changes)},
    )
    await db.commit()
    await db.refresh(rule)
    return rule


@router.delete("/rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_rule(
    rule_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(Permissions.AGENTS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> None:
    await GuardrailRuleRepository(db, ctx.tenant_id).delete(rule_id)
    await log_audit_event(
        db, tenant_id=ctx.tenant_id, actor_user_id=ctx.user_id, action="guardrail_rule.deleted",
        resource_type="guardrail_rule", resource_id=rule_id,
    )
    await db.commit()


@router.get("/events", response_model=list[GuardrailEventRead])
async def list_events(
    rule_kind: str | None = None,
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    ctx: RequestContext = Depends(require_permission(Permissions.AGENTS_READ)),
    db: AsyncSession = Depends(get_db),
) -> list[GuardrailEvent]:
    query = select(GuardrailEvent).where(GuardrailEvent.tenant_id == ctx.tenant_id)
    if rule_kind:
        query = query.where(GuardrailEvent.rule_kind == rule_kind)
    result = await db.execute(
        query.order_by(GuardrailEvent.created_at.desc()).limit(limit).offset(offset)
    )
    return list(result.scalars().all())


@router.get("/knowledge", response_model=list[KnowledgeControlRead])
async def list_knowledge_controls(
    ctx: RequestContext = Depends(require_permission(Permissions.AGENTS_READ)),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    result = await db.execute(
        select(KnowledgeDocument, KnowledgeAIControl)
        .outerjoin(KnowledgeAIControl, KnowledgeAIControl.document_id == KnowledgeDocument.id)
        .where(KnowledgeDocument.tenant_id == ctx.tenant_id)
        .order_by(KnowledgeDocument.created_at.desc())
    )
    return [
        {
            "document_id": doc.id,
            "title": doc.title,
            "chunk_count": doc.chunk_count,
            "enabled": control.enabled if control else True,
            "visibility": control.visibility if control else "customer",
            "updated_at": doc.updated_at,
        }
        for doc, control in result.all()
    ]


@router.put("/knowledge/{doc_id}", response_model=KnowledgeControlRead)
async def set_knowledge_control(
    doc_id: uuid.UUID,
    payload: KnowledgeControlUpdate,
    ctx: RequestContext = Depends(require_permission(Permissions.AGENTS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    doc = (
        await db.execute(
            select(KnowledgeDocument).where(
                KnowledgeDocument.id == doc_id, KnowledgeDocument.tenant_id == ctx.tenant_id
            )
        )
    ).scalar_one_or_none()
    if doc is None:
        raise NotFoundError("KnowledgeDocument not found")
    control = (
        await db.execute(
            select(KnowledgeAIControl).where(
                KnowledgeAIControl.document_id == doc_id, KnowledgeAIControl.tenant_id == ctx.tenant_id
            )
        )
    ).scalar_one_or_none()
    if control is None:
        control = KnowledgeAIControl(tenant_id=ctx.tenant_id, document_id=doc_id)
        db.add(control)
    control.enabled = payload.enabled
    control.visibility = payload.visibility
    await log_audit_event(
        db, tenant_id=ctx.tenant_id, actor_user_id=ctx.user_id, action="knowledge_ai_control.updated",
        resource_type="knowledge_document", resource_id=doc_id,
        extra={"enabled": payload.enabled, "visibility": payload.visibility},
    )
    await db.commit()
    return {
        "document_id": doc.id, "title": doc.title, "chunk_count": doc.chunk_count,
        "enabled": control.enabled, "visibility": control.visibility, "updated_at": doc.updated_at,
    }
