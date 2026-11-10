"""Omnichannel: SMS, email, website live chat, Instagram/Messenger, web
forms, missed calls + voicemail, click-to-call and the unified inbox.

Two kinds of routes live here. Staff routes use the normal JWT context
(RequestContext). Public/webhook routes derive the tenant from something
other than a JWT — the Twilio "To" number (SMS), an unguessable
`public_key` stored in channel_settings (web chat, forms, email) or the
Meta page/account id — and authenticate the *request* by Twilio/Meta signature
or by that key. Treat those lookups as the trust boundary.
"""

import hashlib
import hmac
import json
import logging
import re
import secrets
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from urllib.parse import urlparse

import httpx
from app.api.v1.telephony import _is_blocked, _resolve_auth_token, _validate_request
from app.core.audit import log_audit_event
from app.core.config import get_settings
from app.core.deps import RequestContext, get_db, require_permission
from app.core.exceptions import NotFoundError
from app.core.permissions import Permissions
from app.integrations.channel_payloads import (
    parse_email_payload,
    parse_meta_messages,
    verify_meta_signature,
)
from app.integrations.channel_senders import ChannelSendError
from app.integrations.credentials import get_twilio_credentials
from app.integrations.webchat_widget import WIDGET_JS
from app.integrations.whatsapp import WhatsAppAPIError
from app.schemas.channels import (
    CONFIG_KEYS,
    CREDENTIAL_KEYS,
    AIControlPayload,
    CallbackPayload,
    ChannelSettingsRead,
    ChannelSettingsUpdate,
    ClickToCallPayload,
    ClickToCallRead,
    FollowUpPayload,
    FollowUpResult,
    HandledPayload,
    InboxItem,
    MissedCallRead,
    ReplyPayload,
    SendTemplatePayload,
    ThreadMessage,
    ThreadRead,
    VoicemailRead,
)
from app.services import channels as svc
from app.services import channels_voice as voice
from app.services import chat_form, follow_up, plans, web_call, widget_install, widget_leads
from app.services import privacy as privacy_svc
from app.services import whatsapp_templates as tpl
from app.services.customer_lookup import find_or_create_customer_by_phone
from app.services.outbound_call import OutboundCallError, place_ai_outbound_call
from app.services.webhooks import enqueue_webhook_event
from app.services.whatsapp_inbound import default_agent_id
from app.workflows.dispatch import enqueue_workflow_trigger
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import (
    Call,
    CallDirection,
    CallStatus,
    Conversation,
    ConversationChannel,
    Customer,
    KnowledgeDocument,
    Message,
    MessageDirection,
    MessageSender,
    PhoneNumber,
    Tenant,
    Ticket,
    TicketComment,
    TicketPriority,
    WhatsAppAccount,
    WhatsAppTemplate,
    WorkflowTrigger,
)
from packages.database.models.channels import (
    SETTINGS_CHANNELS,
    ChannelSettings,
    ChannelThread,
    MissedCall,
    Voicemail,
)
from packages.database.models.widget_lead import WidgetLead

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/channels", tags=["channels"])

_EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response/>'
_CHANNEL_LABELS = {
    "sms": "SMS",
    "email": "Email",
    "web_chat": "Website live chat",
    "instagram": "Instagram DMs",
    "messenger": "Facebook Messenger",
    "web_form": "Web forms",
    "voicemail": "Voicemail",
}
_SETUP_NOTES = {
    "sms": "In the Twilio console set the number's 'A message comes in' webhook (HTTP POST) to the URL below. Replies are sent from the same number.",
    "email": "Point your inbound email provider (Mailgun route, SendGrid Inbound Parse, or your own IMAP poller) at the URL below with a POST of JSON or form fields. Replies are sent through the platform SMTP server with a Reply-To of your inbound address.",
    "web_chat": "Paste the snippet before </body> on every page where the chat bubble should appear.",
    "instagram": "In your Meta app add the Instagram webhook with the shared URL below and the verify token, subscribe to 'messages', and store the access token + app secret here. External id is your Instagram business account id.",
    "messenger": "In your Meta app add the Page webhook with the shared URL below and the verify token, subscribe to 'messages', and store the page access token + app secret here. External id is your Facebook page id.",
    "web_form": "POST fields name, email, phone, subject, message (JSON or form-encoded) to the URL below. Include a hidden field named 'website' (left empty) as a bot trap.",
    "voicemail": "When a number you own has no AI agent assigned, callers can leave a message instead of being rejected. Transcripts arrive via Twilio.",
}


# ------------------------------------------------------------------ helpers


def _client_ip(request: Request) -> str:
    if get_settings().trust_x_forwarded_for:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def _read_payload(request: Request) -> dict:
    """JSON or form-encoded body; the widget posts JSON as text/plain."""
    content_type = request.headers.get("content-type", "")
    if "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
        return {k: v for k, v in (await request.form()).items() if isinstance(v, str)}
    raw = await request.body()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Body must be JSON or form data") from exc
    if not isinstance(data, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Body must be a JSON object")
    return data


def _cors_json(request: Request, row: ChannelSettings | None, data: dict, code: int = 200) -> JSONResponse:
    """Public endpoints are called from customers' own websites. Origin
    restrictions come from the channel's `allowed_origins` config."""
    origin = request.headers.get("origin")
    allowed = list((row.config or {}).get("allowed_origins") or []) if row else []
    headers = {"Vary": "Origin"}
    if origin and (not allowed or origin in allowed):
        headers["Access-Control-Allow-Origin"] = origin if allowed else "*"
    return JSONResponse(data, status_code=code, headers=headers)


def _origin_forbidden(request: Request, row: ChannelSettings) -> bool:
    origin = request.headers.get("origin")
    allowed = (row.config or {}).get("allowed_origins") or []
    if origin and origin == _own_origin():
        return False  # the test page served by this API
    return bool(allowed and origin and origin not in allowed)


def _privacy_required(config: dict) -> bool:
    """The privacy notice, age confirmation and consent are on unless the business
    switches them off for a chat that collects nothing about a person."""
    return config.get("privacy_consent") is not False


def _own_origin() -> str:
    parsed = urlparse(get_settings().public_base_url)
    return f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else ""


_WIDGET_TEXT_LIMITS = {
    "subtitle": 80,
    "launcher_label": 30,
    "lead_heading": 80,
    "lead_intro": 200,
    "button_label": 30,
    "fallback_message": 300,
}


def _validate_widget_config(config: dict) -> None:
    for key in ("lead_form", "require_phone", "call_me", "voice_enabled", "show_email", "show_message", "use_knowledge", "kb_only", "privacy_consent"):
        if key in config and not isinstance(config[key], bool):
            raise HTTPException(422, f"{key} must be true or false")
    if config.get("template") not in (None, "", *widget_leads.WIDGET_TEMPLATES):
        raise HTTPException(422, f"template must be one of: {', '.join(widget_leads.WIDGET_TEMPLATES)}")
    if config.get("position") not in (None, "", "left", "right"):
        raise HTTPException(422, "position must be left or right")
    color = config.get("color")
    if color not in (None, "") and not (isinstance(color, str) and re.fullmatch(r"#[0-9a-fA-F]{3,8}", color)):
        raise HTTPException(422, "color must be a hex colour like #2563eb")
    replies = config.get("quick_replies")
    if replies is not None and (
        not isinstance(replies, list)
        or len(replies) > 4
        or not all(isinstance(q, str) and len(q) <= 60 for q in replies)
    ):
        raise HTTPException(422, "quick_replies must be up to 4 suggestions of at most 60 characters")
    for key, limit in _WIDGET_TEXT_LIMITS.items():
        value = config.get(key)
        if value not in (None, "") and not (isinstance(value, str) and len(value) <= limit):
            raise HTTPException(422, f"{key} must be text of at most {limit} characters")
    countries = config.get("allowed_countries")
    if countries is not None and (
        not isinstance(countries, list)
        or len(countries) > 30
        or not all(isinstance(c, str) and re.fullmatch(r"\+?\d{1,4}", c.strip()) for c in countries)
    ):
        raise HTTPException(422, "allowed_countries must be calling codes like +91, one per entry")
    cap = config.get("max_calls_per_day")
    if cap is not None and (isinstance(cap, bool) or not isinstance(cap, int) or not 1 <= cap <= 1000):
        raise HTTPException(422, "max_calls_per_day must be a whole number from 1 to 1000")
    consent = config.get("consent_text")
    if consent is not None and (not isinstance(consent, str) or len(consent) > 600):
        raise HTTPException(422, "consent_text must be text of at most 600 characters")
    if config.get("require_phone") and not config.get("lead_form"):
        raise HTTPException(422, "require_phone needs lead_form to be on")
    number = config.get("call_from_number")
    if number is not None and not (isinstance(number, str) and re.fullmatch(r"(\+\d{7,15})?", number.strip())):
        raise HTTPException(422, "call_from_number must be a phone number like +14155550100")
    if "form_fields" in config:
        try:
            config["form_fields"] = chat_form.clean_fields(config["form_fields"])
        except chat_form.FieldConfigError as exc:
            raise HTTPException(422, str(exc)) from exc


def _msg(m: Message) -> dict:
    return {
        "id": str(m.id),
        "direction": m.direction.value,
        "sender": m.sender.value,
        "text": m.text,
        "created_at": m.created_at.isoformat(),
        "sources": [{"title": c["title"]} for c in (m.sources or [])],
    }


def _snippet(public_key: str) -> str:
    base = get_settings().public_base_url
    return f'<script src="{base}/api/v1/channels/webchat/widget.js" data-key="{public_key}" async></script>'


def _settings_read(channel: str, row: ChannelSettings | None, all_rows: dict[str, ChannelSettings]) -> ChannelSettingsRead:
    urls = svc.webhook_urls({c: (r.public_key if r else None) for c, r in all_rows.items()})
    webhook = {
        "sms": urls["sms"],
        "email": urls.get("email"),
        "instagram": urls["meta"],
        "messenger": urls["meta"],
        "web_form": urls.get("web_form"),
        "voicemail": urls["voicemail"],
    }.get(channel)
    return ChannelSettingsRead(
        channel=channel,
        label=_CHANNEL_LABELS[channel],
        enabled=bool(row and row.enabled),
        public_key=row.public_key if row else None,
        external_id=row.external_id if row else None,
        ai_agent_id=row.ai_agent_id if row else None,
        config=dict(row.config or {}) if row else {},
        credentials_set=sorted(svc.load_credentials(row).keys()) if row else [],
        webhook_url=webhook,
        widget_snippet=_snippet(row.public_key) if channel == "web_chat" and row and row.public_key else None,
        setup_notes=_SETUP_NOTES[channel],
    )


# ------------------------------------------------------------------ settings (staff)


async def _all_settings(db: AsyncSession, tenant_id: uuid.UUID) -> dict[str, ChannelSettings]:
    rows = (
        await db.execute(select(ChannelSettings).where(ChannelSettings.tenant_id == tenant_id))
    ).scalars().all()
    return {r.channel: r for r in rows}


@router.get("/settings", response_model=list[ChannelSettingsRead])
async def list_channel_settings(
    ctx: RequestContext = Depends(require_permission(Permissions.INTEGRATIONS_READ)),
    db: AsyncSession = Depends(get_db),
) -> list[ChannelSettingsRead]:
    rows = await _all_settings(db, ctx.tenant_id)
    return [_settings_read(c, rows.get(c), rows) for c in SETTINGS_CHANNELS]


@router.put("/settings/{channel}", response_model=ChannelSettingsRead)
async def update_channel_settings(
    channel: str,
    payload: ChannelSettingsUpdate,
    ctx: RequestContext = Depends(require_permission(Permissions.INTEGRATIONS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> ChannelSettingsRead:
    if channel not in SETTINGS_CHANNELS:
        raise NotFoundError("Unknown channel")
    rows = await _all_settings(db, ctx.tenant_id)
    row = rows.get(channel)
    if row is None:
        row = ChannelSettings(tenant_id=ctx.tenant_id, channel=channel, enabled=False, config={})
        db.add(row)
        rows[channel] = row

    if payload.config is not None:
        unknown = set(payload.config) - CONFIG_KEYS[channel]
        if unknown:
            raise HTTPException(422, f"Unknown config key(s) for {channel}: {', '.join(sorted(unknown))}")
        origins = payload.config.get("allowed_origins")
        if origins is not None and (
            not isinstance(origins, list) or not all(isinstance(o, str) for o in origins)
        ):
            raise HTTPException(422, "allowed_origins must be a list of origin strings")
        if channel == "web_chat":
            _validate_widget_config(payload.config)
        row.config = {k: v for k, v in payload.config.items() if v not in (None, "")}

    if payload.credentials is not None:
        unknown = set(payload.credentials) - CREDENTIAL_KEYS[channel]
        if unknown:
            raise HTTPException(422, f"Unknown credential(s) for {channel}: {', '.join(sorted(unknown))}")
        merged = svc.load_credentials(row)
        for key, value in payload.credentials.items():
            if value == "":
                merged.pop(key, None)
            else:
                merged[key] = value
        row.encrypted_credentials = svc.dump_credentials(merged) if merged else None

    if payload.ai_agent_id is not None:
        if await svc.resolve_agent(db, ctx.tenant_id, payload.ai_agent_id) is None:
            raise HTTPException(422, "AI agent not found")
        row.ai_agent_id = payload.ai_agent_id
    elif payload.clear_ai_agent:
        row.ai_agent_id = None

    if payload.external_id is not None:
        row.external_id = payload.external_id.strip() or None
        if row.external_id and channel in ("instagram", "messenger"):
            clash = (
                await db.execute(
                    select(ChannelSettings.id).where(
                        ChannelSettings.channel == channel,
                        ChannelSettings.external_id == row.external_id,
                        ChannelSettings.tenant_id != ctx.tenant_id,
                    )
                )
            ).first()
            if clash:
                raise HTTPException(409, "That account id is already connected to another workspace")

    if channel in svc.PUBLIC_KEY_CHANNELS and (row.public_key is None or payload.rotate_public_key):
        row.public_key = svc.new_public_key()

    if payload.enabled is not None:
        if payload.enabled:
            _require_ready(channel, row)
            # A business with exactly one AI agent gets it here automatically (as on WhatsApp). Without
            # this, switching a channel on left visitors talking to nobody and nothing said why.
            if row.ai_agent_id is None and not payload.clear_ai_agent:
                row.ai_agent_id = await default_agent_id(db, ctx.tenant_id)
        row.enabled = payload.enabled

    await db.commit()
    await db.refresh(row)
    return _settings_read(channel, row, rows)


def _require_ready(channel: str, row: ChannelSettings) -> None:
    creds = svc.load_credentials(row)
    if channel in ("instagram", "messenger"):
        missing = [
            n
            for n, ok in (
                ("external_id", row.external_id),
                ("page_access_token", creds.get("page_access_token")),
                ("app_secret", creds.get("app_secret") or get_settings().meta_webhook_secret),
            )
            if not ok
        ]
        if missing:
            raise HTTPException(422, f"Cannot enable {channel}: missing {', '.join(missing)}")
    if channel == "email" and not get_settings().smtp_host:
        raise HTTPException(
            422, "Cannot enable email: the platform SMTP server is not configured (SMTP_HOST)"
        )


# ------------------------------------------------------------------ SMS (Twilio)


@router.post("/sms/webhook")
async def sms_webhook(request: Request, db: AsyncSession = Depends(get_db)) -> Response:
    form = dict((await request.form()).items())
    to_number, from_number = form.get("To", ""), form.get("From", "")
    phone = (
        await db.execute(select(PhoneNumber).where(PhoneNumber.e164_number == to_number))
    ).scalar_one_or_none()
    auth_token = await _resolve_auth_token(db, phone.tenant_id if phone else None)
    await _validate_request(request, form, auth_token=auth_token)

    body = (form.get("Body") or "").strip()
    empty = Response(content=_EMPTY_TWIML, media_type="application/xml")
    if phone is None or not body or not from_number:
        return empty
    sms = await svc.get_channel_settings(db, phone.tenant_id, "sms")
    if sms is None or not sms.enabled or await _is_blocked(db, phone.tenant_id, from_number):
        return empty
    message_sid = form.get("MessageSid") or form.get("SmsSid")
    if await svc.message_already_seen(db, phone.tenant_id, message_sid):
        return empty

    agent = await svc.resolve_agent(db, phone.tenant_id, sms.ai_agent_id or phone.ai_agent_id)
    customer, created = await find_or_create_customer_by_phone(db, phone.tenant_id, from_number)
    conversation, _, _ = await svc.ensure_thread(
        db,
        phone.tenant_id,
        ConversationChannel.SMS,
        f"{from_number}|{to_number}",
        customer_id=customer.id,
        contact_address=from_number,
        via_address=to_number,
    )
    outcome = await svc.process_inbound_text(
        db,
        tenant_id=phone.tenant_id,
        agent=agent,
        conversation=conversation,
        text=body,
        provider_message_id=message_sid,
        customer_created=created,
        source="sms",
    )
    await svc.send_ai_reply(db, outcome)
    return empty


# ------------------------------------------------------------------ email (generic inbound)


@router.post("/email/webhook/{public_key}")
async def email_webhook(public_key: str, request: Request, db: AsyncSession = Depends(get_db)) -> dict:
    row = await svc.get_settings_by_public_key(db, public_key, "email")
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown or disabled email channel")
    secret = svc.load_credentials(row).get("webhook_secret")
    if secret and not hmac.compare_digest(request.headers.get("X-Webhook-Secret", ""), secret):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid webhook secret")

    mail = parse_email_payload(await _read_payload(request))
    if mail is None:
        return {"status": "ignored", "reason": "no sender or body"}
    ours = {get_settings().smtp_from_email.lower(), str((row.config or {}).get("reply_to_address", "")).lower()}
    if mail.is_automated or mail.from_address in ours:
        return {"status": "ignored", "reason": "automated message"}
    tenant_id = row.tenant_id
    if await svc.message_already_seen(db, tenant_id, mail.message_id):
        return {"status": "duplicate"}

    customer, created = await svc.find_or_create_customer_by_email(
        db, tenant_id, mail.from_address, mail.from_name or None
    )
    conversation = None
    conversation_id = await svc.find_email_conversation_id(
        db, tenant_id, [*([mail.in_reply_to] if mail.in_reply_to else []), *mail.references]
    )
    if conversation_id is not None:
        conversation = await db.get(Conversation, conversation_id)
    if conversation is None:
        conversation, _, _ = await svc.ensure_thread(
            db,
            tenant_id,
            ConversationChannel.EMAIL,
            (mail.message_id or f"mail:{uuid.uuid4().hex}")[:255],
            customer_id=customer.id,
            contact_address=mail.from_address,
            via_address=mail.to_address or None,
            subject=mail.subject or "(no subject)",
        )
    agent = await svc.resolve_agent(db, tenant_id, row.ai_agent_id)
    outcome = await svc.process_inbound_text(
        db,
        tenant_id=tenant_id,
        agent=agent,
        conversation=conversation,
        text=mail.text,
        provider_message_id=mail.message_id,
        customer_created=created,
        source="email",
    )
    return {"status": "ok", "conversation_id": str(conversation.id), **await svc.send_ai_reply(db, outcome)}


# ------------------------------------------------------------------ Instagram + Messenger (Meta)


@router.get("/meta/webhook")
async def meta_verify(
    hub_mode: str = Query(alias="hub.mode"),
    hub_verify_token: str = Query(alias="hub.verify_token"),
    hub_challenge: str = Query(alias="hub.challenge"),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Meta's subscribe handshake: echo hub.challenge when the verify token
    matches the platform's or any tenant's configured one."""
    tokens = {get_settings().meta_whatsapp_verify_token}
    rows = (
        await db.execute(
            select(ChannelSettings).where(ChannelSettings.channel.in_(("instagram", "messenger")))
        )
    ).scalars().all()
    for row in rows:
        token = svc.load_credentials(row).get("verify_token")
        if token:
            tokens.add(token)
    if hub_mode != "subscribe" or not any(secrets.compare_digest(hub_verify_token, t) for t in tokens):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid verify token")
    return PlainTextResponse(hub_challenge)


@router.post("/meta/webhook")
async def meta_webhook(request: Request, db: AsyncSession = Depends(get_db)) -> dict:
    raw = await request.body()
    try:
        body = json.loads(raw)
    except ValueError as exc:
        raise HTTPException(400, "Invalid JSON") from exc
    signature = request.headers.get("X-Hub-Signature-256")
    handled = 0
    for message in parse_meta_messages(body):
        row = (
            await db.execute(
                select(ChannelSettings).where(
                    ChannelSettings.channel == message.channel,
                    ChannelSettings.external_id == message.account_id,
                )
            )
        ).scalar_one_or_none()
        if row is None or not row.enabled:
            continue
        creds = svc.load_credentials(row)
        secret = creds.get("app_secret") or get_settings().meta_webhook_secret
        if not secret:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"{message.channel} app secret is not configured; cannot verify the webhook",
            )
        if not verify_meta_signature(raw, signature, secret):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid webhook signature")
        if await svc.message_already_seen(db, row.tenant_id, message.message_id):
            continue

        channel = ConversationChannel(message.channel)
        thread = await svc.find_thread(db, row.tenant_id, channel, message.sender_id)
        created = False
        if thread is None:
            customer = Customer(
                tenant_id=row.tenant_id,
                name=f"{_CHANNEL_LABELS[message.channel]} user {message.sender_id[-6:]}",
                extra={f"{message.channel}_user_id": message.sender_id},
            )
            db.add(customer)
            await db.flush()
            created = True
            customer_id = customer.id
        else:
            customer_id = None
        conversation, _, _ = await svc.ensure_thread(
            db,
            row.tenant_id,
            channel,
            message.sender_id,
            customer_id=customer_id,
            contact_address=message.sender_id,
            via_address=message.account_id,
        )
        agent = await svc.resolve_agent(db, row.tenant_id, row.ai_agent_id)
        outcome = await svc.process_inbound_text(
            db,
            tenant_id=row.tenant_id,
            agent=agent,
            conversation=conversation,
            text=message.text,
            provider_message_id=message.message_id,
            customer_created=created,
            source=message.channel,
        )
        await svc.send_ai_reply(db, outcome)
        handled += 1
    return {"status": "ok", "handled": handled}


class KnowledgeCheckPayload(BaseModel):
    question: str = Field(min_length=1, max_length=500)


@router.post("/web-chat/knowledge-check")
async def webchat_knowledge_check(
    payload: KnowledgeCheckPayload,
    ctx: RequestContext = Depends(require_permission(Permissions.INTEGRATIONS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """What the website chat's AI would be given from the knowledge base for this question.
    Lets a business test its documents without spending a model call."""
    hits = await svc.knowledge_hits_for_visitors(db, ctx.tenant_id, payload.question, limit=4)
    total = (
        await db.execute(
            select(func.count()).select_from(KnowledgeDocument).where(KnowledgeDocument.tenant_id == ctx.tenant_id)
        )
    ).scalar_one()
    return {
        "documents": int(total),
        "matches": [{"title": h["title"], "excerpt": h["content"][:400], "score": round(h["score"], 3)} for h in hits],
    }


@router.get("/web-chat/status")
async def webchat_install_status(
    ctx: RequestContext = Depends(require_permission(Permissions.INTEGRATIONS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Which websites have loaded the widget lately; lets the setup wizard confirm an install."""
    row = (await _all_settings(db, ctx.tenant_id)).get("web_chat")
    if row is None or not row.public_key:
        return {"connected": False, "sites": [], "test_url": None}
    sites = await widget_install.seen_sites(row.public_key)
    base = get_settings().public_base_url.rstrip("/")
    return {"connected": bool(sites), "sites": sites[:5], "test_url": f"{base}/api/v1/channels/webchat/try/{row.public_key}"}


# ------------------------------------------------------------------ website live chat (public)


_PREVIEW_HTML = (
    '<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>'
    '<body style="margin:0;background:#eceff4"><div style="padding:28px 32px;font-family:system-ui,sans-serif">'
    '<div style="height:12px;width:90px;border-radius:6px;background:#d9dee6;margin-bottom:26px"></div>'
    '<div style="height:26px;width:62%;border-radius:8px;background:#cbd2dc;margin-bottom:12px"></div>'
    '<div style="height:12px;width:80%;border-radius:6px;background:#d9dee6;margin-bottom:8px"></div>'
    '<div style="height:12px;width:68%;border-radius:6px;background:#d9dee6;margin-bottom:26px"></div>'
    '<div style="display:flex;gap:14px"><div style="flex:1;height:96px;border-radius:12px;background:#dde2ea"></div>'
    '<div style="flex:1;height:96px;border-radius:12px;background:#dde2ea"></div></div></div>'
    '<script src="/api/v1/channels/webchat/widget.js" data-key="preview" data-preview></script></body></html>'
)


_TRY_HTML = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
    "<title>Try your chat widget</title></head>"
    '<body style="margin:0;font-family:system-ui,sans-serif;background:#f6f7f9;color:#0f172a">'
    '<div style="max-width:560px;margin:72px auto;padding:0 24px"><h1 style="font-size:22px;margin:0 0 8px">Your chat widget</h1>'
    '<p style="color:#475569;line-height:1.5;margin:0 0 6px">This is a private test page. The chat bubble in the corner is your real widget, '
    "so anything you submit here appears in your Chat leads.</p></div>"
    '<script src="/api/v1/channels/webchat/widget.js" data-key="__KEY__"></script></body></html>'
)


@router.get("/webchat/try/{public_key}", include_in_schema=False)
async def webchat_try_page(public_key: str, db: AsyncSession = Depends(get_db)) -> Response:
    """A plain page with the real widget on it, for trying the chat before installing it on a site."""
    row = await svc.get_settings_by_public_key(db, public_key, "web_chat")
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Chat is not available")
    return Response(
        _TRY_HTML.replace("__KEY__", public_key),
        media_type="text/html",
        headers={"Content-Security-Policy": "default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src data:; frame-ancestors 'none'", "X-Robots-Tag": "noindex"},
    )


@router.get("/webchat/preview", include_in_schema=False)
async def webchat_preview() -> Response:
    """A page that shows the chat widget with demo data. The Chat widget designer frames it,
    so only the app's own origins may do so; it never talks to the database."""
    ancestors = " ".join(get_settings().cors_allow_origins) or "'none'"
    return Response(
        _PREVIEW_HTML,
        media_type="text/html",
        headers={"Content-Security-Policy": f"default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; frame-ancestors {ancestors}"},
    )


@router.get("/webchat/widget.js")
async def webchat_widget() -> Response:
    return Response(
        content=WIDGET_JS,
        media_type="application/javascript",
        headers={"Cache-Control": "public, max-age=300", "Access-Control-Allow-Origin": "*"},
    )


async def _webchat_row(db: AsyncSession, public_key: str) -> ChannelSettings:
    row = await svc.get_settings_by_public_key(db, public_key, "web_chat")
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Chat is not available")
    return row


def _session_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def _webchat_conversation(
    db: AsyncSession, row: ChannelSettings, token: str | None
) -> tuple[Conversation, ChannelThread] | None:
    if not token or len(token) > 200:
        return None
    thread = await svc.find_thread(db, row.tenant_id, ConversationChannel.WEB_CHAT, _session_hash(token))
    if thread is None:
        return None
    conversation = await db.get(Conversation, thread.conversation_id)
    return (conversation, thread) if conversation else None


@router.get("/webchat/{public_key}/config")
async def webchat_config(public_key: str, request: Request, db: AsyncSession = Depends(get_db)) -> JSONResponse:
    row = await svc.get_settings_by_public_key(db, public_key, "web_chat")
    if row is None:
        return _cors_json(request, None, {"detail": "Chat is not available"}, 404)
    config = row.config or {}
    tenant = await db.get(Tenant, row.tenant_id)
    await widget_install.note_seen(
        public_key,
        widget_install.site_of(request.headers.get("origin"), request.headers.get("referer")),
        own_origin=_own_origin(),
    )
    privacy = None
    if _privacy_required(config) and tenant is not None:
        notice = await privacy_svc.public_notice(db, row.tenant_id, tenant.name)
        privacy = {
            "required": True,
            "version": notice["version"],
            "lines": notice["lines"],
            "url": privacy_svc.public_url(tenant.slug),
        }
        await db.commit()
    return _cors_json(
        request,
        row,
        {
            "title": config.get("title") or "Chat with us",
            "color": config.get("color") or "#2563eb",
            **widget_leads.widget_options(config, tenant.name if tenant else "us"),
            "fields": chat_form.effective_fields(config),
            "privacy": privacy,
        },
    )


@router.post("/webchat/{public_key}/session")
async def webchat_session(public_key: str, request: Request, db: AsyncSession = Depends(get_db)) -> JSONResponse:
    row = await svc.get_settings_by_public_key(db, public_key, "web_chat")
    if row is None:
        return _cors_json(request, None, {"detail": "Chat is not available"}, 404)
    if _origin_forbidden(request, row):
        return _cors_json(request, row, {"detail": "This website is not allowed to use the chat"}, 403)
    if await svc.hit_rate_limit(f"wc-session:{public_key}:{_client_ip(request)}", limit=10):
        return _cors_json(request, row, {"detail": "Too many chats started; try again in a minute"}, 429)

    try:
        payload = await _read_payload(request)
    except HTTPException:
        payload = {}
    config = row.config or {}
    if _privacy_required(config) and not (
        payload.get("privacy_accepted") is True and payload.get("adult_confirmed") is True
    ):
        return _cors_json(
            request, row, {"detail": "Please read and accept the privacy notice to start the chat."}, 422
        )
    email = str(payload.get("email") or "").strip()[:254]
    name = str(payload.get("name") or "").strip()[:120] or None
    capture = bool(config.get("lead_form"))
    custom_fields = chat_form.configured_fields(config) if capture else []
    collected = None
    phone = None
    if custom_fields:
        if payload.get("website"):  # the bot trap, as below
            return _cors_json(request, row, {"session_token": secrets.token_urlsafe(32), "messages": []})
        tenant = await db.get(Tenant, row.tenant_id)
        try:
            collected = chat_form.collect_answers(
                custom_fields, payload.get("answers"), tenant.country if tenant else None
            )
        except widget_leads.LeadError as exc:
            return _cors_json(request, row, {"detail": exc.message}, exc.http_status)
        name, phone, email = collected.name, collected.phone, collected.email or ""
    elif capture:
        # A honeypot field that real visitors never see; bots fill it.
        if payload.get("website"):
            return _cors_json(request, row, {"session_token": secrets.token_urlsafe(32), "messages": []})
        if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            return _cors_json(request, row, {"detail": "That email address doesn't look right."}, 422)
        tenant = await db.get(Tenant, row.tenant_id)
        try:
            phone = widget_leads.normalize_visitor_phone(
                str(payload.get("phone") or ""), tenant.country if tenant else None
            )
        except widget_leads.LeadError as exc:
            return _cors_json(request, row, {"detail": exc.message}, exc.http_status)
        if not name:
            return _cors_json(request, row, {"detail": "Please tell us your name."}, 422)
        if config.get("require_phone") and not phone:
            return _cors_json(request, row, {"detail": "Please enter your phone number."}, 422)
        if not phone and not email:
            return _cors_json(request, row, {"detail": "Please give us a phone number or an email address."}, 422)
    customer_id = None
    if email and not capture and re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        customer, _ = await svc.find_or_create_customer_by_email(db, row.tenant_id, email, name)
        customer_id = customer.id

    token = secrets.token_urlsafe(32)
    conversation, _, _ = await svc.ensure_thread(
        db,
        row.tenant_id,
        ConversationChannel.WEB_CHAT,
        _session_hash(token),
        customer_id=customer_id,
        contact_address=phone or email or None,
        subject="Website chat",
    )
    if capture:
        utm = payload.get("utm") if isinstance(payload.get("utm"), dict) else {}
        lead, customer = await widget_leads.capture_lead(
            db,
            row,
            conversation_id=conversation.id,
            name=name,
            phone=phone,
            email=email or None,
            interest=(collected.interest if collected else str(payload.get("interest") or "").strip()[:2000]) or None,
            answers=collected.answers if collected else None,
            require_contact=collected is None,
            page_url=str(payload.get("page_url") or "")[:2048] or None,
            referrer=str(payload.get("referrer") or "")[:2048] or None,
            utm={k: v for k, v in utm.items() if k.startswith("utm_")},
            user_agent=request.headers.get("user-agent"),
            ip=_client_ip(request),
        )
        conversation.customer_id = customer.id if customer else None
        enqueue_webhook_event(
            tenant_id=row.tenant_id,
            event="lead.captured",
            data={
                "lead_id": str(lead.crm_lead_id) if lead.crm_lead_id else None,
                "customer_id": str(lead.customer_id) if lead.customer_id else None,
                "source": "web_chat",
                "name": lead.name,
                "phone": lead.phone,
                "email": lead.email,
                "page_url": lead.page_url,
                "answers": lead.answers,
            },
        )
    if _privacy_required(config):
        tenant_row = await db.get(Tenant, row.tenant_id)
        await privacy_svc.record_consent(
            db,
            row.tenant_id,
            purposes=["enquiry"],
            source="web_chat",
            business=tenant_row.name if tenant_row else "us",
            name=name,
            phone=phone,
            email=email or None,
            customer_id=conversation.customer_id,
            ip=_client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
    messages = []
    greeting = (row.config or {}).get("greeting")
    if greeting:
        messages.append(
            await svc.record_message(
                db,
                row.tenant_id,
                conversation.id,
                direction=MessageDirection.OUTBOUND,
                sender=MessageSender.AI_AGENT if row.ai_agent_id else MessageSender.HUMAN_AGENT,
                text=str(greeting)[:1000],
            )
        )
    await db.commit()
    return _cors_json(
        request, row, {"session_token": token, "messages": [_msg(m) for m in messages]}
    )


@router.post("/webchat/{public_key}/message")
async def webchat_message(public_key: str, request: Request, db: AsyncSession = Depends(get_db)) -> JSONResponse:
    row = await svc.get_settings_by_public_key(db, public_key, "web_chat")
    if row is None:
        return _cors_json(request, None, {"detail": "Chat is not available"}, 404)
    if _origin_forbidden(request, row):
        return _cors_json(request, row, {"detail": "This website is not allowed to use the chat"}, 403)
    try:
        payload = await _read_payload(request)
    except HTTPException as exc:
        return _cors_json(request, row, {"detail": exc.detail}, exc.status_code)

    token = str(payload.get("session_token") or "")
    text = str(payload.get("text") or "").strip()
    if not text:
        return _cors_json(request, row, {"detail": "Message is empty"}, 422)
    if len(text) > 2000:
        return _cors_json(request, row, {"detail": "Message is too long (max 2000 characters)"}, 422)
    if await svc.hit_rate_limit(f"wc-msg:{_session_hash(token)}", limit=20) or await svc.hit_rate_limit(
        f"wc-ip:{_client_ip(request)}", limit=60
    ):
        return _cors_json(request, row, {"detail": "You are sending messages too fast"}, 429)

    found = await _webchat_conversation(db, row, token)
    if found is None:
        return _cors_json(request, row, {"detail": "Chat session expired"}, 404)
    conversation, _ = found
    started = datetime.now(UTC)
    agent = await svc.resolve_agent(db, row.tenant_id, row.ai_agent_id)
    chat_options = dict(row.config or {})
    visitor = (
        await db.execute(select(WidgetLead.answers).where(WidgetLead.conversation_id == conversation.id))
    ).scalar_one_or_none()
    chat_options["_visitor_context"] = chat_form.visitor_context(visitor)
    await svc.process_inbound_text(
        db,
        tenant_id=row.tenant_id,
        agent=agent,
        conversation=conversation,
        text=text,
        provider_message_id=None,
        source="web_chat",
        chat_options=chat_options,
    )
    new_messages = (
        await db.execute(
            select(Message)
            .where(Message.conversation_id == conversation.id, Message.created_at >= started)
            .order_by(Message.created_at)
        )
    ).scalars().all()
    return _cors_json(
        request,
        row,
        {
            "messages": [_msg(m) for m in new_messages],
            "awaiting_human": agent is None,
            # The visitor asked for a call: the widget shows a "Call me" card and
            # waits for their explicit tap, which is also their consent.
            "call_offer": bool((row.config or {}).get("call_me")) and widget_leads.wants_call(text),
        },
    )


@router.post("/webchat/{public_key}/call-request")
async def webchat_call_request(public_key: str, request: Request, db: AsyncSession = Depends(get_db)) -> JSONResponse:
    """The visitor asks the AI agent to phone them. Needs their explicit consent."""
    row = await svc.get_settings_by_public_key(db, public_key, "web_chat")
    if row is None:
        return _cors_json(request, None, {"detail": "Chat is not available"}, 404)
    # A detached copy for the response headers: the database row can be expired by a rollback below.
    cors_row = SimpleNamespace(config=dict(row.config or {}))
    if _origin_forbidden(request, row):
        return _cors_json(request, cors_row, {"detail": "This website is not allowed to use the chat"}, 403)
    try:
        payload = await _read_payload(request)
    except HTTPException as exc:
        return _cors_json(request, cors_row, {"detail": exc.detail}, exc.status_code)

    token = str(payload.get("session_token") or "")
    if await svc.hit_rate_limit(f"wc-call-ip:{_client_ip(request)}", limit=3, window_seconds=3600) or (
        await svc.hit_rate_limit(f"wc-call-sess:{_session_hash(token)}", limit=3, window_seconds=3600)
    ):
        return _cors_json(request, cors_row, {"detail": "Too many call requests. Please try again later."}, 429)
    found = await _webchat_conversation(db, row, token)
    if found is None:
        return _cors_json(request, cors_row, {"detail": "Chat session expired"}, 404)
    conversation, _ = found

    try:
        lead = (
            await db.execute(select(WidgetLead).where(WidgetLead.conversation_id == conversation.id))
        ).scalar_one_or_none()
        if lead is None:
            # No lead form was used: the details come with the request itself.
            tenant = await db.get(Tenant, row.tenant_id)
            name = str(payload.get("name") or "").strip()[:120] or None
            phone = widget_leads.normalize_visitor_phone(str(payload.get("phone") or ""), tenant.country if tenant else None)
            if not name or not phone:
                raise widget_leads.LeadError("Please enter your name and phone number.")
            lead, customer = await widget_leads.capture_lead(
                db,
                row,
                conversation_id=conversation.id,
                name=name,
                phone=phone,
                email=None,
                interest=None,
                page_url=str(payload.get("page_url") or "")[:2048] or None,
                referrer=None,
                utm=None,
                user_agent=request.headers.get("user-agent"),
                ip=_client_ip(request),
            )
            conversation.customer_id = customer.id if customer else None
        result = await widget_leads.request_call(
            db,
            row,
            lead,
            phone=str(payload.get("phone") or "") or None,
            consent=payload.get("consent") is True,
            note=str(payload.get("note") or "").strip()[:500] or None,
            ip=_client_ip(request),
        )
    except widget_leads.LeadError as exc:
        # Nothing was committed on this path, so the request's session is simply discarded.
        return _cors_json(request, cors_row, {"detail": exc.message}, exc.http_status)
    except Exception:
        # Always answer with CORS headers, or the visitor's browser only shows "Failed to fetch".
        logger.exception("web chat call request failed for key %s", public_key)
        return _cors_json(request, cors_row, {"detail": "Something went wrong. Please try again."}, 500)
    return _cors_json(request, cors_row, result)


@router.get("/webchat/{public_key}/messages")
async def webchat_messages(
    public_key: str,
    request: Request,
    session_token: str,
    after: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Polled by the widget so a human agent's reply shows up."""
    row = await svc.get_settings_by_public_key(db, public_key, "web_chat")
    if row is None:
        return _cors_json(request, None, {"detail": "Chat is not available"}, 404)
    if await svc.hit_rate_limit(f"wc-poll:{_session_hash(session_token)}", limit=40):
        return _cors_json(request, row, {"detail": "Polling too fast"}, 429)
    found = await _webchat_conversation(db, row, session_token)
    if found is None:
        return _cors_json(request, row, {"detail": "Chat session expired"}, 404)
    query = select(Message).where(Message.conversation_id == found[0].id)
    if after:
        try:
            query = query.where(Message.created_at > datetime.fromisoformat(after))
        except ValueError:
            return _cors_json(request, row, {"detail": "Invalid 'after' timestamp"}, 422)
    messages = (await db.execute(query.order_by(Message.created_at).limit(200))).scalars().all()
    return _cors_json(request, row, {"messages": [_msg(m) for m in messages]})


# ------------------------------------------------------------------ web forms (public)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE_RE = re.compile(r"^\+?[0-9][0-9\s\-().]{5,28}$")


@router.post("/forms/{public_key}")
async def submit_web_form(public_key: str, request: Request, db: AsyncSession = Depends(get_db)) -> JSONResponse:
    row = await svc.get_settings_by_public_key(db, public_key, "web_form")
    if row is None:
        return _cors_json(request, None, {"detail": "Form is not available"}, 404)
    if _origin_forbidden(request, row):
        return _cors_json(request, row, {"detail": "This website is not allowed to submit the form"}, 403)
    if await svc.hit_rate_limit(f"form:{public_key}:{_client_ip(request)}", limit=5) or await svc.hit_rate_limit(
        f"form-key:{public_key}", limit=60
    ):
        return _cors_json(request, row, {"detail": "Too many submissions; try again later"}, 429)
    try:
        fields = await _read_payload(request)
    except HTTPException as exc:
        return _cors_json(request, row, {"detail": exc.detail}, exc.status_code)

    config = row.config or {}
    ok_message = config.get("success_message") or "Thanks, we have received your message."
    elapsed = None
    try:
        if fields.get("_ts"):
            elapsed = datetime.now(UTC).timestamp() - float(fields["_ts"]) / 1000
    except (TypeError, ValueError):
        elapsed = None
    spam = svc.evaluate_form_spam(fields, elapsed_seconds=elapsed)
    if spam == "honeypot field filled":
        # Look successful so the bot has nothing to adapt to; nothing is stored.
        return _cors_json(request, row, {"status": "ok", "message": ok_message})
    if spam:
        return _cors_json(request, row, {"detail": "Submission rejected"}, 422)

    name = str(fields.get("name") or "").strip()
    email = str(fields.get("email") or "").strip().lower()
    phone = re.sub(r"[\s\-().]", "", str(fields.get("phone") or "")) or None
    if email and not _EMAIL_RE.match(email):
        return _cors_json(request, row, {"detail": "Email address is not valid"}, 422)
    if phone and not _PHONE_RE.match(phone):
        return _cors_json(request, row, {"detail": "Phone number is not valid"}, 422)
    if not email and not phone:
        return _cors_json(request, row, {"detail": "Provide an email address or a phone number"}, 422)

    tenant_id = row.tenant_id
    if email:
        customer, created = await svc.find_or_create_customer_by_email(db, tenant_id, email, name or None)
        if phone and not customer.phone:
            customer.phone = phone
    else:
        customer, created = await find_or_create_customer_by_phone(db, tenant_id, phone)
        if name and customer.name == customer.phone:
            customer.name = name[:255]

    message = str(fields.get("message")).strip()
    subject = str(fields.get("subject") or "").strip() or f"Web form: {name or email or phone}"
    try:
        priority = TicketPriority(config.get("ticket_priority", "medium"))
    except ValueError:
        priority = TicketPriority.MEDIUM
    ticket = Ticket(tenant_id=tenant_id, customer_id=customer.id, subject=subject[:255], priority=priority)
    db.add(ticket)
    await db.flush()
    from app.services.operations import apply_sla_safely

    await apply_sla_safely(db, tenant_id, ticket)
    db.add(
        TicketComment(
            tenant_id=tenant_id,
            ticket_id=ticket.id,
            body=f"{message}\n\nFrom: {name or '-'} | {email or '-'} | {phone or '-'}\nSource: web form",
        )
    )
    conversation, _, _ = await svc.ensure_thread(
        db,
        tenant_id,
        ConversationChannel.WEB_FORM,
        f"ticket:{ticket.id}",
        customer_id=customer.id,
        contact_address=email or phone,
        subject=subject,
    )
    await svc.record_message(
        db,
        tenant_id,
        conversation.id,
        direction=MessageDirection.INBOUND,
        sender=MessageSender.CUSTOMER,
        text=message,
    )
    await db.commit()

    ticket_data = {
        "ticket_id": str(ticket.id),
        "customer_id": str(customer.id),
        "priority": priority.value,
        "source": "web_form",
    }
    enqueue_webhook_event(tenant_id=tenant_id, event="ticket.created", data=ticket_data)
    enqueue_workflow_trigger(tenant_id=tenant_id, trigger=WorkflowTrigger.TICKET_CREATED, payload=ticket_data)
    if created:
        enqueue_workflow_trigger(
            tenant_id=tenant_id,
            trigger=WorkflowTrigger.CUSTOMER_CREATED,
            payload={"customer_id": str(customer.id), "source": "web_form"},
        )

    callback = None
    if config.get("callback_followup") and phone and phone.startswith("+"):
        try:
            call = await place_ai_outbound_call(
                db, tenant_id, to_number=phone, ai_agent_id=str(row.ai_agent_id) if row.ai_agent_id else None
            )
            callback = {"requested": True, "call_id": str(call.id)}
        except OutboundCallError as exc:
            callback = {"requested": False, "error": str(exc)}
    return _cors_json(
        request,
        row,
        {"status": "ok", "message": ok_message, **({"callback": callback} if callback else {})},
    )


# ------------------------------------------------------------------ missed calls + voicemail


def _voicemail_read(v: Voicemail, customer_name: str | None, customer_id: uuid.UUID | None) -> VoicemailRead:
    return VoicemailRead(
        id=v.id,
        missed_call_id=v.missed_call_id,
        customer_id=customer_id,
        customer_name=customer_name,
        from_number=v.from_number,
        to_number=v.to_number,
        recording_url=v.recording_url,
        duration_seconds=v.duration_seconds,
        transcription=v.transcription,
        transcription_status=v.transcription_status,
        handled=v.handled,
        handled_at=v.handled_at,
        created_at=v.created_at,
    )


async def _missed_for_sid(db: AsyncSession, call_sid: str | None) -> MissedCall | None:
    if not call_sid:
        return None
    return (
        await db.execute(
            select(MissedCall)
            .where(MissedCall.provider_call_sid == call_sid, MissedCall.reason == "voicemail")
            .order_by(MissedCall.created_at.desc())
            .limit(1)
        )
    ).scalars().first()


@router.post("/voicemail/recording")
async def voicemail_recording(request: Request, db: AsyncSession = Depends(get_db)) -> Response:
    form = dict((await request.form()).items())
    missed = await _missed_for_sid(db, form.get("CallSid"))
    await _validate_request(
        request, form, auth_token=await _resolve_auth_token(db, missed.tenant_id if missed else None)
    )
    if missed is not None and form.get("RecordingStatus", "completed") == "completed":
        duration = form.get("RecordingDuration", "")
        await voice.upsert_voicemail(
            db,
            missed,
            recording_sid=form.get("RecordingSid"),
            recording_url=form.get("RecordingUrl"),
            duration=int(duration) if duration.isdigit() else None,
        )
    return PlainTextResponse("")


@router.post("/voicemail/transcription")
async def voicemail_transcription(request: Request, db: AsyncSession = Depends(get_db)) -> Response:
    form = dict((await request.form()).items())
    missed = await _missed_for_sid(db, form.get("CallSid"))
    await _validate_request(
        request, form, auth_token=await _resolve_auth_token(db, missed.tenant_id if missed else None)
    )
    if missed is not None:
        done = form.get("TranscriptionStatus") == "completed"
        await voice.upsert_voicemail(
            db,
            missed,
            recording_sid=form.get("RecordingSid"),
            recording_url=form.get("RecordingUrl"),
            transcription=form.get("TranscriptionText") if done else None,
            transcription_status="completed" if done else "failed",
        )
    return PlainTextResponse("")


@router.get("/missed-calls", response_model=list[MissedCallRead])
async def list_missed_calls(
    handled: bool | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    ctx: RequestContext = Depends(require_permission(Permissions.CALLS_READ)),
    db: AsyncSession = Depends(get_db),
) -> list[MissedCallRead]:
    query = (
        select(MissedCall, Customer.name)
        .outerjoin(Customer, Customer.id == MissedCall.customer_id)
        .where(MissedCall.tenant_id == ctx.tenant_id)
    )
    if handled is not None:
        query = query.where(MissedCall.handled == handled)
    rows = (
        await db.execute(query.order_by(MissedCall.created_at.desc()).limit(limit).offset(offset))
    ).all()
    ids = [m.id for m, _ in rows]
    voicemails: dict[uuid.UUID, Voicemail] = {}
    if ids:
        for v in (
            await db.execute(select(Voicemail).where(Voicemail.missed_call_id.in_(ids)))
        ).scalars():
            voicemails[v.missed_call_id] = v
    out = []
    for missed, name in rows:
        item = MissedCallRead.model_validate(
            {**{c: getattr(missed, c) for c in MissedCallRead.model_fields if hasattr(missed, c)}, "customer_name": name}
        )
        v = voicemails.get(missed.id)
        item.voicemail = _voicemail_read(v, name, missed.customer_id) if v else None
        out.append(item)
    return out


async def _get_missed(db: AsyncSession, tenant_id: uuid.UUID, missed_id: uuid.UUID) -> MissedCall:
    missed = await db.get(MissedCall, missed_id)
    if missed is None or missed.tenant_id != tenant_id:
        raise NotFoundError("Missed call not found")
    return missed


@router.post("/missed-calls/{missed_id}/handled", response_model=MissedCallRead)
async def set_missed_call_handled(
    missed_id: uuid.UUID,
    payload: HandledPayload,
    ctx: RequestContext = Depends(require_permission(Permissions.CALLS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> MissedCallRead:
    missed = await _get_missed(db, ctx.tenant_id, missed_id)
    voice.mark_handled(missed, ctx.user_id, payload.handled)
    await db.commit()
    name = (await db.get(Customer, missed.customer_id)).name if missed.customer_id else None
    return MissedCallRead.model_validate(
        {**{c: getattr(missed, c) for c in MissedCallRead.model_fields if hasattr(missed, c)}, "customer_name": name}
    )


@router.post("/missed-calls/{missed_id}/callback", response_model=ClickToCallRead)
async def callback_missed_call(
    missed_id: uuid.UUID,
    payload: CallbackPayload,
    ctx: RequestContext = Depends(require_permission(Permissions.CALLS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> ClickToCallRead:
    missed = await _get_missed(db, ctx.tenant_id, missed_id)
    if not missed.from_number or not missed.from_number.startswith("+"):
        raise HTTPException(422, "This caller's number is not available to call back")
    call = await _place_call(
        db, ctx.tenant_id, to_number=missed.from_number, mode=payload.mode,
        ai_agent_id=payload.ai_agent_id, from_number=payload.from_number or missed.to_number,
        agent_phone=payload.agent_phone,
    )
    missed.callback_call_id = call.id
    voice.mark_handled(missed, ctx.user_id, True)
    await db.commit()
    return ClickToCallRead(call_id=call.id, status=call.status.value, mode=payload.mode)


@router.get("/voicemails", response_model=list[VoicemailRead])
async def list_voicemails(
    handled: bool | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    ctx: RequestContext = Depends(require_permission(Permissions.CALLS_READ)),
    db: AsyncSession = Depends(get_db),
) -> list[VoicemailRead]:
    query = (
        select(Voicemail, Customer.name, Customer.id)
        .outerjoin(MissedCall, MissedCall.id == Voicemail.missed_call_id)
        .outerjoin(Customer, Customer.id == MissedCall.customer_id)
        .where(Voicemail.tenant_id == ctx.tenant_id)
    )
    if handled is not None:
        query = query.where(Voicemail.handled == handled)
    rows = (
        await db.execute(query.order_by(Voicemail.created_at.desc()).limit(limit).offset(offset))
    ).all()
    return [_voicemail_read(v, name, cid) for v, name, cid in rows]


@router.get("/voicemails/{voicemail_id}/recording")
async def get_voicemail_recording(
    voicemail_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(Permissions.CALLS_READ)),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Streams the recording through the API: Twilio recording URLs need the
    account credentials, which a browser must not have. Audit-logged."""
    voicemail = await db.get(Voicemail, voicemail_id)
    if voicemail is None or voicemail.tenant_id != ctx.tenant_id:
        raise NotFoundError("Voicemail not found")
    if not voicemail.recording_url:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No recording for this voicemail")
    if urlparse(voicemail.recording_url).hostname != "api.twilio.com":
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Unsupported recording location")
    credentials = await get_twilio_credentials(db, ctx.tenant_id)
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as http:
        upstream = await http.get(
            voicemail.recording_url, auth=(credentials.account_sid, credentials.auth_token)
        )
    if upstream.status_code != 200:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Recording is not available yet")
    await log_audit_event(
        db,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        action="voicemail.recording_accessed",
        resource_type="voicemail",
        resource_id=voicemail.id,
    )
    await db.commit()
    return Response(content=upstream.content, media_type="audio/mpeg")


@router.post("/voicemails/{voicemail_id}/handled", response_model=VoicemailRead)
async def set_voicemail_handled(
    voicemail_id: uuid.UUID,
    payload: HandledPayload,
    ctx: RequestContext = Depends(require_permission(Permissions.CALLS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> VoicemailRead:
    voicemail = await db.get(Voicemail, voicemail_id)
    if voicemail is None or voicemail.tenant_id != ctx.tenant_id:
        raise NotFoundError("Voicemail not found")
    voice.mark_handled(voicemail, ctx.user_id, payload.handled)
    name = cid = None
    if voicemail.missed_call_id:
        missed = await db.get(MissedCall, voicemail.missed_call_id)
        if missed and missed.customer_id:
            cid = missed.customer_id
            customer = await db.get(Customer, cid)
            name = customer.name if customer else None
        if missed and payload.handled:
            voice.mark_handled(missed, ctx.user_id, True)
    await db.commit()
    return _voicemail_read(voicemail, name, cid)


# ------------------------------------------------------------------ web calls (browser voice)


@router.get("/webcall/demo", include_in_schema=False)
async def webcall_demo(request: Request) -> JSONResponse:
    """Whether the marketing site offers a "Talk to the AI" button, and the public key it calls with."""
    key = get_settings().web_call_demo_key
    return JSONResponse({"enabled": bool(key), "key": key or None})


@router.get("/webcall/{public_key}/config")
async def webcall_config(public_key: str, request: Request, db: AsyncSession = Depends(get_db)) -> JSONResponse:
    row = await svc.get_settings_by_public_key(db, public_key, "web_chat")
    if row is None or not (row.config or {}).get("voice_enabled") or row.ai_agent_id is None:
        return _cors_json(request, None, {"enabled": False}, 404)
    config = row.config or {}
    return _cors_json(request, row, {"enabled": True, "title": config.get("title") or "Talk to us"})


@router.post("/webcall/{public_key}/start")
async def webcall_start(public_key: str, request: Request, db: AsyncSession = Depends(get_db)) -> JSONResponse:
    """Starts a call from the visitor's browser. Creates the Call row and returns where to connect."""
    row = await svc.get_settings_by_public_key(db, public_key, "web_chat")
    if row is None or not (row.config or {}).get("voice_enabled") or row.ai_agent_id is None:
        return _cors_json(request, None, {"detail": "Voice calls are not available"}, 404)
    if _origin_forbidden(request, row):
        return _cors_json(request, row, {"detail": "This website is not allowed to start calls"}, 403)
    # A call costs real money, so starts are limited per visitor and per key.
    if await svc.hit_rate_limit(f"wcall:{public_key}:{_client_ip(request)}", limit=4, window_seconds=600):
        return _cors_json(request, row, {"detail": "Too many calls started; please try again in a few minutes"}, 429)
    if await svc.hit_rate_limit(f"wcall-all:{public_key}", limit=60, window_seconds=600):
        return _cors_json(request, row, {"detail": "We are taking a lot of calls right now; please try again soon"}, 429)
    try:
        await plans.ensure_inbound_allowed(db, row.tenant_id)
    except plans.PlanLimitError:
        return _cors_json(request, row, {"detail": "Calls are not available right now. Please try again later."}, 402)

    call = Call(
        tenant_id=row.tenant_id,
        ai_agent_id=row.ai_agent_id,
        direction=CallDirection.INBOUND,
        status=CallStatus.RINGING,
        from_number="web",
        to_number="web",
        started_at=datetime.now(UTC),
    )
    db.add(call)
    await db.flush()
    await db.commit()
    return _cors_json(
        request,
        row,
        {
            "call_id": str(call.id),
            "ws_url": web_call.gateway_url(str(call.id)),
            "expires_in": web_call.TTL_SECONDS,
        },
        201,
    )


# ------------------------------------------------------------------ click-to-call


async def _place_call(
    db: AsyncSession, tenant_id: uuid.UUID, *, to_number: str, mode: str,
    ai_agent_id: uuid.UUID | None, from_number: str | None, agent_phone: str | None,
) -> Call:
    try:
        return await voice.place_click_to_call(
            db, tenant_id, to_number=to_number, mode=mode,
            ai_agent_id=str(ai_agent_id) if ai_agent_id else None,
            from_number=from_number, agent_phone=agent_phone,
        )
    except OutboundCallError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc


@router.post("/click-to-call", response_model=ClickToCallRead, status_code=status.HTTP_201_CREATED)
async def click_to_call(
    payload: ClickToCallPayload,
    ctx: RequestContext = Depends(require_permission(Permissions.CALLS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> ClickToCallRead:
    call = await _place_call(
        db, ctx.tenant_id, to_number=payload.to_number, mode=payload.mode,
        ai_agent_id=payload.ai_agent_id, from_number=payload.from_number, agent_phone=payload.agent_phone,
    )
    return ClickToCallRead(call_id=call.id, status=call.status.value, mode=payload.mode)


@router.post("/click-to-call/bridge")
async def click_to_call_bridge(
    call_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)
) -> Response:
    """Twilio fetches this once the staff member picks up, to dial the customer."""
    form = dict((await request.form()).items())
    call = await db.get(Call, call_id)
    if call is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Call not found")
    await _validate_request(request, form, auth_token=await _resolve_auth_token(db, call.tenant_id))
    if call.started_at is None:
        call.started_at = datetime.now(UTC)
        await db.commit()
    return Response(
        content=voice.bridge_twiml(customer_number=call.to_number or "", caller_id=call.from_number),
        media_type="application/xml",
    )


# ------------------------------------------------------------------ unified inbox


@router.get("/inbox", response_model=list[InboxItem])
async def inbox(
    channel: str | None = None,
    unread_only: bool = False,
    q: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=30, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    ctx: RequestContext = Depends(require_permission(Permissions.CONVERSATIONS_READ)),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    if channel and channel not in {c.value for c in ConversationChannel if c != ConversationChannel.INTERNAL}:
        raise HTTPException(422, f"Unknown channel '{channel}'")
    return await svc.list_inbox(
        db, ctx.tenant_id, channel=channel, unread_only=unread_only, search=q, limit=limit, offset=offset
    )


async def _tenant_conversation(db: AsyncSession, tenant_id: uuid.UUID, conversation_id: uuid.UUID) -> Conversation:
    conversation = await db.get(Conversation, conversation_id)
    if (
        conversation is None
        or conversation.tenant_id != tenant_id
        or conversation.channel == ConversationChannel.INTERNAL
    ):
        raise NotFoundError("Conversation not found")
    return conversation


_NO_REPLY = {
    ConversationChannel.VOICE: "Voice calls cannot be replied to here; use click-to-call instead.",
    ConversationChannel.INTERNAL: "Internal conversations cannot be replied to.",
}


@router.get("/conversations/{conversation_id}", response_model=ThreadRead)
async def get_thread(
    conversation_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(Permissions.CONVERSATIONS_READ)),
    db: AsyncSession = Depends(get_db),
) -> ThreadRead:
    conversation = await _tenant_conversation(db, ctx.tenant_id, conversation_id)


    summary = await _inbox_item(db, ctx.tenant_id, conversation)
    messages = (
        await db.execute(
            select(Message)
            .where(Message.conversation_id == conversation.id)
            .order_by(Message.created_at)
            .limit(500)
        )
    ).scalars().all()
    hint = _NO_REPLY.get(conversation.channel)
    return ThreadRead(
        conversation=summary,
        messages=[ThreadMessage.model_validate(m, from_attributes=True) for m in messages],
        can_reply=hint is None,
        reply_hint=hint,
        ai_paused_until=conversation.ai_paused_until if svc.ai_paused(conversation) else None,
        whatsapp_window_open=(
            tpl.window_open(
                max((m.created_at for m in messages if m.direction == MessageDirection.INBOUND), default=None)
            )
            if conversation.channel == ConversationChannel.WHATSAPP
            else None
        ),
    )


async def _inbox_item(db: AsyncSession, tenant_id: uuid.UUID, conversation: Conversation) -> InboxItem:
    # Reuse the inbox query (unread math included) narrowed to this conversation.
    items = await svc.list_inbox(db, tenant_id, conversation_id=conversation.id, limit=1)
    if not items:
        raise NotFoundError("Conversation not found")
    return InboxItem.model_validate(items[0])


@router.post("/conversations/{conversation_id}/read", status_code=status.HTTP_204_NO_CONTENT)
async def mark_read(
    conversation_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(Permissions.CONVERSATIONS_READ)),
    db: AsyncSession = Depends(get_db),
) -> Response:
    conversation = await _tenant_conversation(db, ctx.tenant_id, conversation_id)
    await svc.mark_conversation_read(db, conversation)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/conversations/{conversation_id}/reply", response_model=ThreadMessage, status_code=status.HTTP_201_CREATED)
async def reply(
    conversation_id: uuid.UUID,
    payload: ReplyPayload,
    ctx: RequestContext = Depends(require_permission(Permissions.CONVERSATIONS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> Message:
    conversation = await _tenant_conversation(db, ctx.tenant_id, conversation_id)
    text = payload.text.strip()
    if not text:
        raise HTTPException(422, "Reply is empty")
    try:
        provider_id = await svc.deliver_reply(db, conversation, text)
    except ChannelSendError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    message = await svc.record_message(
        db,
        ctx.tenant_id,
        conversation.id,
        direction=MessageDirection.OUTBOUND,
        sender=MessageSender.HUMAN_AGENT,
        text=text,
        provider_message_id=provider_id,
    )
    # A person answered: the AI steps back so the two don't talk over each other.
    svc.pause_ai(conversation)
    await svc.mark_conversation_read(db, conversation)
    await db.commit()
    return message


@router.post("/conversations/{conversation_id}/template", response_model=ThreadMessage, status_code=status.HTTP_201_CREATED)
async def send_template_to_conversation(
    conversation_id: uuid.UUID,
    payload: SendTemplatePayload,
    ctx: RequestContext = Depends(require_permission(Permissions.CONVERSATIONS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> Message:
    """Re-open a WhatsApp chat after 24h of customer silence with an approved
    template. Blanks are filled from the customer record and their next booking."""
    conversation = await _tenant_conversation(db, ctx.tenant_id, conversation_id)
    if conversation.channel != ConversationChannel.WHATSAPP:
        raise HTTPException(422, "Templates are only for WhatsApp conversations")
    template = await db.get(WhatsAppTemplate, payload.template_id)
    if template is None or template.tenant_id != ctx.tenant_id:
        raise HTTPException(404, "Template not found")
    customer = await db.get(Customer, conversation.customer_id) if conversation.customer_id else None
    thread = await svc.get_thread(db, conversation.id)
    to = (thread.contact_address if thread else None) or (customer.phone if customer else None)
    if not to:
        raise HTTPException(422, "The customer has no phone number")
    from app.services.customer_context import next_appointment

    appointment = await next_appointment(db, ctx.tenant_id, customer.id) if customer else None
    values = await tpl.context_values(db, ctx.tenant_id, customer, appointment=appointment, extra=payload.values)
    accounts = (await db.execute(select(WhatsAppAccount).where(WhatsAppAccount.tenant_id == ctx.tenant_id))).scalars().all()
    account = next((a for a in accounts if thread and a.phone_number_id == thread.via_address), None)
    try:
        await plans.ensure_whatsapp_available(db, ctx.tenant_id)
        # Saved below as a Message, which is what gets counted.
        provider_id = await tpl.send_template(
            db, ctx.tenant_id, to=to, template=template, values=values, account=account, metered=False
        )
    except (tpl.TemplateError, WhatsAppAPIError) as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    message = await svc.record_message(
        db,
        ctx.tenant_id,
        conversation.id,
        direction=MessageDirection.OUTBOUND,
        sender=MessageSender.HUMAN_AGENT,
        text=tpl.render(template, values),
        provider_message_id=provider_id,
    )
    svc.pause_ai(conversation)
    await db.commit()
    return message


@router.get("/conversations/{conversation_id}/follow-up")
async def follow_up_options(
    conversation_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(Permissions.CONVERSATIONS_READ)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Which ways of reaching this customer (WhatsApp, SMS, email) work right now, and why not if one doesn't."""
    conversation = await _tenant_conversation(db, ctx.tenant_id, conversation_id)
    return await follow_up.options(db, ctx.tenant_id, conversation)


@router.post(
    "/conversations/{conversation_id}/follow-up",
    response_model=FollowUpResult,
    status_code=status.HTTP_201_CREATED,
)
async def send_follow_up(
    conversation_id: uuid.UUID,
    payload: FollowUpPayload,
    ctx: RequestContext = Depends(require_permission(Permissions.CONVERSATIONS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> FollowUpResult:
    """After a call: message the customer on WhatsApp, SMS or email. It is saved in that channel's own
    conversation, so their answer comes back to the same place."""
    conversation = await _tenant_conversation(db, ctx.tenant_id, conversation_id)
    try:
        message, target = await follow_up.send(
            db,
            ctx.tenant_id,
            conversation,
            via=payload.via,
            text=payload.text,
            subject=payload.subject,
            template_id=payload.template_id,
            values=payload.values,
        )
    except follow_up.FollowUpError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    await db.commit()
    return FollowUpResult(
        conversation_id=target.id,
        channel=payload.via,
        message=ThreadMessage.model_validate(message, from_attributes=True),
    )


@router.post("/conversations/{conversation_id}/ai", response_model=ThreadRead)
async def set_conversation_ai(
    conversation_id: uuid.UUID,
    payload: AIControlPayload,
    ctx: RequestContext = Depends(require_permission(Permissions.CONVERSATIONS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> ThreadRead:
    """active=false: staff take over (AI quiet for 12h, renewed by each staff
    reply). active=true: hand the conversation back to the AI agent."""
    conversation = await _tenant_conversation(db, ctx.tenant_id, conversation_id)
    if payload.active:
        svc.resume_ai(conversation)
    else:
        svc.pause_ai(conversation)
    await db.commit()
    return await get_thread(conversation_id, ctx, db)
