"""WhatsApp message templates for a business: create from scratch or from the
starter library, check against Meta's rules, submit for approval, follow the
approval status, delete. See app/services/whatsapp_templates.py."""

import uuid
from datetime import datetime
from typing import Literal

from app.core.deps import RequestContext, get_db, require_permission
from app.core.permissions import Permissions
from app.integrations.whatsapp import WhatsAppAPIError
from app.services import whatsapp_templates as tpl
from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import WhatsAppAccount, WhatsAppTemplate

router = APIRouter(prefix="/whatsapp-templates", tags=["whatsapp"])


class TemplateButton(BaseModel):
    type: Literal["QUICK_REPLY", "URL", "PHONE_NUMBER"] = "QUICK_REPLY"
    text: str = Field(min_length=1, max_length=25)
    url: str | None = None
    phone_number: str | None = None


class TemplateIn(BaseModel):
    name: str | None = Field(default=None, max_length=512)
    language: str = Field(default="en", max_length=16)
    category: Literal["UTILITY", "MARKETING", "AUTHENTICATION"] = "UTILITY"
    purpose: str = Field(default="custom", max_length=32)
    header_text: str | None = Field(default=None, max_length=60)
    body: str = Field(min_length=1, max_length=1024)
    footer_text: str | None = Field(default=None, max_length=60)
    buttons: list[TemplateButton] = Field(default_factory=list, max_length=10)
    examples: dict[str, str] = Field(default_factory=dict)
    whatsapp_account_id: uuid.UUID | None = None
    submit: bool = False  # send to Meta right away


class TemplateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    whatsapp_account_id: uuid.UUID | None
    name: str
    language: str
    category: str
    purpose: str
    header_text: str | None
    body: str
    footer_text: str | None
    buttons: list[dict]
    variables: list[str]
    examples: dict
    status: str
    rejection_reason: str | None
    meta_template_id: str | None
    created_at: datetime
    updated_at: datetime
    problems: list[str] = []
    preview: str = ""


def _out(t: WhatsAppTemplate) -> TemplateOut:
    out = TemplateOut.model_validate(t)
    out.problems = tpl.validate(t)
    out.preview = tpl.render(t, {})
    return out


async def _get(db: AsyncSession, tenant_id: uuid.UUID, template_id: uuid.UUID) -> WhatsAppTemplate:
    t = await db.get(WhatsAppTemplate, template_id)
    if t is None or t.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Template not found")
    return t


def _apply(t: WhatsAppTemplate, payload: TemplateIn) -> None:
    t.name = tpl.slugify_name(payload.name or payload.purpose or "template")
    t.language = payload.language
    t.category = payload.category
    t.purpose = payload.purpose if payload.purpose in tpl.PURPOSES else "custom"
    t.header_text = (payload.header_text or "").strip() or None
    t.body = payload.body.strip()
    t.footer_text = (payload.footer_text or "").strip() or None
    t.buttons = [b.model_dump(exclude_none=True) for b in payload.buttons]
    t.examples = {k.lower(): v for k, v in payload.examples.items() if v}
    t.variables = tpl.compile_body(t.body)[1]
    if payload.whatsapp_account_id:
        t.whatsapp_account_id = payload.whatsapp_account_id


async def _submit(db: AsyncSession, t: WhatsAppTemplate) -> None:
    """Keep the business's work: the draft is saved first, so a refusal from Meta
    (or a broken connection) never loses what they wrote."""
    await db.commit()
    try:
        await tpl.submit(db, t)
    except tpl.TemplateError as exc:
        await db.rollback()
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Saved as a draft, but not sent: {exc}") from exc


async def _check_account(db: AsyncSession, tenant_id: uuid.UUID, account_id: uuid.UUID | None) -> None:
    if account_id is None:
        return
    account = await db.get(WhatsAppAccount, account_id)
    if account is None or account.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "That WhatsApp number isn't connected")


@router.get("/library")
async def template_library(_: RequestContext = Depends(require_permission(Permissions.CONVERSATIONS_READ))) -> dict:
    """Starter templates, the named blanks you can use, and what each purpose means."""
    return {"library": tpl.LIBRARY, "variables": tpl.VARIABLES, "purposes": tpl.PURPOSES}


@router.get("", response_model=list[TemplateOut])
async def list_templates(
    sync: bool = False,
    ctx: RequestContext = Depends(require_permission(Permissions.CONVERSATIONS_READ)),
    db: AsyncSession = Depends(get_db),
) -> list[TemplateOut]:
    if sync:
        if await tpl.sync_from_meta(db, ctx.tenant_id):
            await db.commit()
    rows = (
        await db.execute(
            select(WhatsAppTemplate)
            .where(WhatsAppTemplate.tenant_id == ctx.tenant_id)
            .order_by(WhatsAppTemplate.updated_at.desc())
        )
    ).scalars().all()
    return [_out(t) for t in rows]


@router.post("", response_model=TemplateOut, status_code=status.HTTP_201_CREATED)
async def create_template(
    payload: TemplateIn,
    ctx: RequestContext = Depends(require_permission(Permissions.CONVERSATIONS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> TemplateOut:
    await _check_account(db, ctx.tenant_id, payload.whatsapp_account_id)
    t = WhatsAppTemplate(tenant_id=ctx.tenant_id, status="DRAFT")
    _apply(t, payload)
    db.add(t)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"You already have a template called '{t.name}' in this language. Pick another name."
        ) from exc
    if payload.submit:
        await _submit(db, t)
    await db.commit()
    await db.refresh(t)
    return _out(t)


@router.put("/{template_id}", response_model=TemplateOut)
async def update_template(
    template_id: uuid.UUID,
    payload: TemplateIn,
    ctx: RequestContext = Depends(require_permission(Permissions.CONVERSATIONS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> TemplateOut:
    t = await _get(db, ctx.tenant_id, template_id)
    if t.status not in tpl.EDITABLE:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Meta has this template already. To change it, duplicate it under a new name and submit that.",
        )
    await _check_account(db, ctx.tenant_id, payload.whatsapp_account_id)
    _apply(t, payload)
    if t.status == "REJECTED":
        t.status, t.meta_template_id = "DRAFT", None
    if payload.submit:
        await _submit(db, t)
    await db.commit()
    await db.refresh(t)
    return _out(t)


@router.post("/{template_id}/submit", response_model=TemplateOut)
async def submit_template(
    template_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(Permissions.CONVERSATIONS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> TemplateOut:
    t = await _get(db, ctx.tenant_id, template_id)
    if t.status not in tpl.EDITABLE:
        raise HTTPException(status.HTTP_409_CONFLICT, "This template was already sent to Meta.")
    await _submit(db, t)
    await db.commit()
    await db.refresh(t)
    return _out(t)


@router.post("/sync", response_model=list[TemplateOut])
async def sync_templates(
    ctx: RequestContext = Depends(require_permission(Permissions.CONVERSATIONS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> list[TemplateOut]:
    return await list_templates(True, ctx, db)


@router.delete("/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_template(
    template_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(Permissions.CONVERSATIONS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> Response:
    t = await _get(db, ctx.tenant_id, template_id)
    try:
        await tpl.delete_at_meta(db, t)
    except (tpl.TemplateError, WhatsAppAPIError) as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Meta didn't delete it: {exc}") from exc
    await db.delete(t)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
