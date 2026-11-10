"""WhatsApp: tenant-facing account connection + the Meta webhook (shared
across all tenants — see WhatsAppAccount docstring)."""

import hmac
import uuid

from app.core.config import get_settings
from app.core.crypto import encrypt_secret
from app.core.deps import RequestContext, get_db, require_permission
from app.core.permissions import Permissions
from app.core.repository import TenantScopedRepository
from app.integrations.whatsapp import check_account_details, verify_webhook_signature
from app.schemas.whatsapp import WhatsAppAccountConnect, WhatsAppAccountRead, WhatsAppAccountUpdate
from app.services import whatsapp_inbound as wa
from app.services import whatsapp_templates as tpl
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import AIAgent, WhatsAppAccount

router = APIRouter(tags=["whatsapp"])


class WhatsAppAccountRepository(TenantScopedRepository[WhatsAppAccount]):
    model = WhatsAppAccount


@router.get("/whatsapp-accounts", response_model=list[WhatsAppAccountRead])
async def list_whatsapp_accounts(
    ctx: RequestContext = Depends(require_permission(Permissions.CONVERSATIONS_READ)),
    db: AsyncSession = Depends(get_db),
) -> list[WhatsAppAccount]:
    return await WhatsAppAccountRepository(db, ctx.tenant_id).list()


@router.patch("/whatsapp-accounts/{account_id}", response_model=WhatsAppAccountRead)
async def update_whatsapp_account(
    account_id: uuid.UUID,
    payload: WhatsAppAccountUpdate,
    ctx: RequestContext = Depends(require_permission(Permissions.CONVERSATIONS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> WhatsAppAccount:
    """Choose which AI agent answers this WhatsApp number (null = staff only)."""
    account = await WhatsAppAccountRepository(db, ctx.tenant_id).get(account_id)
    if payload.ai_agent_id is not None:
        agent = await db.get(AIAgent, payload.ai_agent_id)
        if agent is None or agent.tenant_id != ctx.tenant_id:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "That AI agent doesn't exist")
    account.ai_agent_id = payload.ai_agent_id
    await db.commit()
    await db.refresh(account)
    return account


@router.delete("/whatsapp-accounts/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect_whatsapp_account(
    account_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(Permissions.CONVERSATIONS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Stops CallPlus using this number. Past conversations stay in the inbox."""
    account = await WhatsAppAccountRepository(db, ctx.tenant_id).get(account_id)
    await db.delete(account)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/whatsapp-accounts", response_model=WhatsAppAccountRead, status_code=status.HTTP_201_CREATED
)
async def connect_whatsapp_account(
    payload: WhatsAppAccountConnect,
    ctx: RequestContext = Depends(require_permission(Permissions.CONVERSATIONS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> WhatsAppAccount:
    problem = await check_account_details(payload.waba_id, payload.phone_number_id, payload.access_token)
    if problem:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, problem)
    account = await WhatsAppAccountRepository(db, ctx.tenant_id).create(
        phone_number_id=payload.phone_number_id,
        waba_id=payload.waba_id,
        display_phone_number=payload.display_phone_number,
        encrypted_access_token=encrypt_secret(payload.access_token),
        ai_agent_id=await wa.default_agent_id(db, ctx.tenant_id),
    )
    await db.commit()
    await db.refresh(account)
    return account


@router.get("/webhooks/whatsapp/meta")
async def verify_whatsapp_webhook(
    hub_mode: str = Query(alias="hub.mode"),
    hub_verify_token: str = Query(alias="hub.verify_token"),
    hub_challenge: str = Query(alias="hub.challenge"),
) -> Response:
    settings = get_settings()
    if hub_mode != "subscribe" or not hmac.compare_digest(
        hub_verify_token, settings.meta_whatsapp_verify_token
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid verify token")
    return Response(content=hub_challenge, media_type="text/plain")


@router.post("/webhooks/whatsapp/meta")
async def receive_whatsapp_webhook(
    request: Request, background: BackgroundTasks, db: AsyncSession = Depends(get_db)
) -> dict:
    """Acknowledge Meta right away; the AI reply happens in the background.
    Meta re-sends notifications it considers failed or slow, so each message id
    is claimed once and repeats are dropped."""
    raw_body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")
    if not verify_webhook_signature(raw_body, signature):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid webhook signature")

    body = await request.json()
    messages, statuses = wa.parse_webhook(body)
    receipts = await wa.apply_statuses(db, statuses) if statuses else 0
    # Meta's verdict on a submitted template (APPROVED / REJECTED / PAUSED ...).
    template_updates = 0
    for entry in body.get("entry") or []:
        for change in entry.get("changes") or []:
            if change.get("field") == "message_template_status_update":
                template_updates += await tpl.apply_status_update(db, change.get("value") or {})
    if template_updates:
        await db.commit()
    queued = 0
    for message in messages:
        if await wa.claim(message.message_id):
            background.add_task(wa.process_message_safely, message)
            queued += 1
    return {"status": "ok", "queued": queued, "receipts": receipts}
