"""WhatsApp Cloud API inbound pipeline.

The webhook only parses, de-duplicates and acknowledges (Meta re-sends a
notification it thinks failed or was slow, which used to produce double AI
replies). The slow part — voice-note transcription, the AI turn and sending the
reply — runs afterwards in `process_message` with its own DB session.

Every message type gets an answer: text, voice notes (transcribed with Sarvam),
button / list taps (their label is the text), and photos, documents, locations
and contacts (stored as a short label; the agent says a team member will look).
"""

import logging
import uuid
from dataclasses import dataclass

from app.core.config import get_settings
from app.integrations.credentials import get_sarvam_api_key
from app.integrations.sarvam import SarvamAPIError, transcribe_audio
from app.integrations.whatsapp import WhatsAppAPIError, download_media, friendly_error, mark_read
from app.services import channels as svc
from app.services.customer_lookup import find_or_create_customer_by_phone
from redis.asyncio import Redis, from_url
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import (
    AgentStatus,
    AIAgent,
    ChannelThread,
    Conversation,
    ConversationChannel,
    Message,
    MessageDirection,
    WhatsAppAccount,
)
from packages.database.session import session_scope

logger = logging.getLogger(__name__)

SEEN_TTL_SECONDS = 3 * 24 * 3600  # Meta retries for up to a few days

_ATTACHMENT_LABELS = {
    "image": "Photo",
    "video": "Video",
    "document": "Document",
    "sticker": "Sticker",
}


@dataclass
class InboundMessage:
    phone_number_id: str
    message_id: str
    from_number: str
    kind: str
    text: str  # what we can already tell without downloading anything
    media_id: str | None = None
    mime_type: str | None = None
    profile_name: str | None = None


@dataclass
class DeliveryStatus:
    message_id: str
    status: str
    error: str | None


def _label(kind: str, caption: str | None) -> str:
    base = f"[{_ATTACHMENT_LABELS.get(kind, kind.title())}]"
    return f"{base} {caption.strip()}" if caption and caption.strip() else base


def parse_webhook(body: dict) -> tuple[list[InboundMessage], list[DeliveryStatus]]:
    messages: list[InboundMessage] = []
    statuses: list[DeliveryStatus] = []
    for entry in body.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            phone_number_id = (value.get("metadata") or {}).get("phone_number_id")
            names = {
                c.get("wa_id"): (c.get("profile") or {}).get("name")
                for c in value.get("contacts") or []
                if isinstance(c, dict)
            }
            for st in value.get("statuses") or []:
                errors = st.get("errors") or []
                error = None
                if errors:
                    e = errors[0]
                    detail = (e.get("error_data") or {}).get("details") or e.get("message") or e.get("title") or ""
                    error = friendly_error(e.get("code"), detail)
                if st.get("id") and st.get("status"):
                    statuses.append(DeliveryStatus(st["id"], str(st["status"])[:16], error))
            for m in value.get("messages") or []:
                if not phone_number_id or not m.get("from"):
                    continue
                if not m.get("id"):
                    m = {**m, "id": f"noid-{uuid.uuid4().hex}"}  # Meta always sends one; keeps hand-made payloads working
                kind = m.get("type") or "unknown"
                text, media_id, mime = "", None, None
                if kind == "text":
                    text = (m.get("text") or {}).get("body") or ""
                elif kind == "interactive":
                    i = m.get("interactive") or {}
                    reply = i.get("button_reply") or i.get("list_reply") or {}
                    text = reply.get("title") or reply.get("description") or ""
                elif kind == "button":
                    text = (m.get("button") or {}).get("text") or ""
                elif kind == "audio":
                    a = m.get("audio") or {}
                    media_id, mime = a.get("id"), a.get("mime_type")
                    text = "[Voice note]"
                elif kind in _ATTACHMENT_LABELS:
                    a = m.get(kind) or {}
                    media_id, mime = a.get("id"), a.get("mime_type")
                    text = _label(kind, a.get("caption") or a.get("filename"))
                elif kind == "location":
                    loc = m.get("location") or {}
                    where = loc.get("name") or loc.get("address") or f"{loc.get('latitude')}, {loc.get('longitude')}"
                    text = f"[Location] {where}"
                elif kind == "contacts":
                    people = ", ".join(
                        (c.get("name") or {}).get("formatted_name") or "contact" for c in m.get("contacts") or []
                    )
                    text = f"[Contact card] {people}"
                elif kind == "reaction":
                    continue  # an emoji on an earlier message: nothing to answer
                else:
                    text = "[Unsupported message]"
                messages.append(
                    InboundMessage(
                        phone_number_id=phone_number_id,
                        message_id=m["id"],
                        from_number=m["from"],
                        kind=kind,
                        text=text,
                        media_id=media_id,
                        mime_type=mime,
                        profile_name=names.get(m["from"]),
                    )
                )
    return messages, statuses


# ------------------------------------------------------------------ de-duplication

_redis: Redis | None = None


def _redis_client() -> Redis:
    global _redis
    if _redis is None:
        _redis = from_url(get_settings().redis_url, decode_responses=True)
    return _redis


async def claim(message_id: str) -> bool:
    """True the first time a WhatsApp message id is seen. If Redis is down we
    fall back to the database check in process_message."""
    try:
        return bool(await _redis_client().set(f"wa:seen:{message_id}", "1", nx=True, ex=SEEN_TTL_SECONDS))
    except Exception:  # noqa: BLE001 - a Redis outage must not drop customer messages
        logger.warning("WhatsApp de-dup: Redis unavailable, relying on the database check")
        return True


# ------------------------------------------------------------------ delivery receipts


async def apply_statuses(db: AsyncSession, statuses: list[DeliveryStatus]) -> int:
    """Sent / delivered / read / failed for messages we sent. Never moves a
    message backwards (a late "sent" after "read" is ignored)."""
    order = {"sent": 1, "delivered": 2, "read": 3, "failed": 4}
    updated = 0
    for st in statuses:
        message = (
            await db.execute(
                select(Message).where(
                    Message.provider_message_id == st.message_id[:128],
                    Message.direction == MessageDirection.OUTBOUND,
                )
            )
        ).scalars().first()
        if message is None:
            continue
        if order.get(st.status, 0) >= order.get(message.delivery_status or "", 0):
            message.delivery_status = st.status
            if st.error:
                message.delivery_error = st.error[:1000]
            updated += 1
    if updated:
        await db.commit()
    return updated


# ------------------------------------------------------------------ inbound processing


async def default_agent_id(db: AsyncSession, tenant_id: uuid.UUID) -> uuid.UUID | None:
    """A business with exactly one AI agent gets it on WhatsApp automatically."""
    ids = (
        await db.execute(
            select(AIAgent.id).where(AIAgent.tenant_id == tenant_id, AIAgent.status != AgentStatus.DISABLED).limit(2)
        )
    ).scalars().all()
    return ids[0] if len(ids) == 1 else None


async def _conversation(
    db: AsyncSession, account: WhatsAppAccount, customer_id: uuid.UUID, from_number: str
) -> Conversation:
    """One thread per (customer number, business number). Conversations made
    before threads existed are adopted instead of starting a fresh history."""
    thread = await svc.find_thread(db, account.tenant_id, ConversationChannel.WHATSAPP, from_number)
    if thread is None:
        legacy = (
            await db.execute(
                select(Conversation)
                .where(
                    Conversation.tenant_id == account.tenant_id,
                    Conversation.customer_id == customer_id,
                    Conversation.channel == ConversationChannel.WHATSAPP,
                )
                .order_by(Conversation.created_at)
                .limit(1)
            )
        ).scalar_one_or_none()
        if legacy is not None:
            db.add(
                ChannelThread(
                    tenant_id=account.tenant_id,
                    conversation_id=legacy.id,
                    channel=ConversationChannel.WHATSAPP.value,
                    external_id=from_number,
                    contact_address=from_number,
                    via_address=account.phone_number_id,
                )
            )
            await db.flush()
            return legacy
    conversation, thread, _ = await svc.ensure_thread(
        db,
        account.tenant_id,
        ConversationChannel.WHATSAPP,
        from_number,
        customer_id=customer_id,
        contact_address=from_number,
        via_address=account.phone_number_id,
    )
    if thread.via_address != account.phone_number_id:
        thread.via_address = account.phone_number_id  # reply from the number they last wrote to
    return conversation


async def _resolve_text(
    db: AsyncSession, account: WhatsAppAccount, message: InboundMessage
) -> tuple[str, str | None]:
    if message.kind != "audio" or not message.media_id:
        return message.text, None
    try:
        audio, mime = await download_media(account, message.media_id)
        api_key = await get_sarvam_api_key(db, account.tenant_id)
        transcript, language = await transcribe_audio(
            audio, message.mime_type or mime, api_key=api_key
        )
    except (WhatsAppAPIError, SarvamAPIError, KeyError) as exc:
        logger.warning("voice note %s not transcribed: %s", message.message_id, exc)
        return "[Voice note - could not be transcribed]", None
    if not transcript:
        return "[Voice note - empty]", None
    return f"[Voice note] {transcript}", language


async def process_message(message: InboundMessage) -> dict:
    """Runs after the webhook has answered Meta: store, (maybe) let the AI answer, send."""
    async with session_scope() as db:
        account = (
            await db.execute(
                select(WhatsAppAccount).where(WhatsAppAccount.phone_number_id == message.phone_number_id)
            )
        ).scalar_one_or_none()
        if account is None:
            return {"handled": False, "reason": "unknown number"}
        if await svc.message_already_seen(db, account.tenant_id, message.message_id):
            return {"handled": False, "reason": "duplicate"}

        await mark_read(account, message.message_id)
        customer, created = await find_or_create_customer_by_phone(db, account.tenant_id, message.from_number)
        if message.profile_name and (not customer.name or customer.name == customer.phone):
            customer.name = message.profile_name[:200]
        conversation = await _conversation(db, account, customer.id, message.from_number)

        # No agent chosen = staff answer from the inbox (a deliberate setting, not a gap:
        # numbers get the business's agent automatically when they're connected).
        agent = None if svc.ai_paused(conversation) else await svc.resolve_agent(db, account.tenant_id, account.ai_agent_id)
        text, caller_language = await _resolve_text(db, account, message)
        outcome = await svc.process_inbound_text(
            db,
            tenant_id=account.tenant_id,
            agent=agent,
            conversation=conversation,
            text=text,
            provider_message_id=message.message_id,
            customer_created=created,
            source="whatsapp",
            media_url=f"whatsapp-media:{message.media_id}" if message.media_id else None,
            caller_language=caller_language,
        )
        sent = await svc.send_ai_reply(db, outcome)
        return {"handled": True, "ai": agent is not None, **sent}


async def process_message_safely(message: InboundMessage) -> None:
    try:
        await process_message(message)
    except Exception:  # noqa: BLE001 - background task: log, never crash the worker loop
        logger.exception("WhatsApp message %s failed", message.message_id)
