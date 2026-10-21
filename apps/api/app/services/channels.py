"""Omnichannel core: per-tenant channel settings, thread bookkeeping, the
shared inbound->AI->reply pipeline, reply routing and the unified inbox query.

Mirrors the WhatsApp flow (app/api/v1/whatsapp.py): resolve tenant -> find or
create customer + conversation -> run_turn -> escalate -> commit -> fire
workflow/webhook events -> send the reply.
"""

import json
import logging
import re
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.core.config import get_settings
from app.core.crypto import decrypt_secret, encrypt_secret
from app.core.rate_limit import _redis
from app.integrations.channel_senders import (
    ChannelSendError,
    send_email_message,
    send_meta_message,
    send_sms_message,
)
from app.integrations.whatsapp import WhatsAppAPIError, send_text_message
from app.services import csat, plans
from app.services.agent_engine import run_turn
from app.services.knowledge import citations as knowledge_citations
from app.services.knowledge import search as knowledge_search
from app.services.webhooks import enqueue_webhook_event
from app.workflows.dispatch import enqueue_workflow_trigger
from redis.exceptions import RedisError
from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import (
    AIAgent,
    Conversation,
    ConversationChannel,
    Customer,
    Message,
    MessageDirection,
    MessageSender,
    Ticket,
    TicketPriority,
    WhatsAppAccount,
    WorkflowTrigger,
)
from packages.database.models.channels import ChannelSettings, ChannelThread

logger = logging.getLogger(__name__)

PUBLIC_KEY_CHANNELS = ("web_chat", "web_form", "email")

# ---------------------------------------------------------------- settings


def new_public_key() -> str:
    return secrets.token_urlsafe(24)


def load_credentials(row: ChannelSettings | None) -> dict:
    if row is None or not row.encrypted_credentials:
        return {}
    return json.loads(decrypt_secret(row.encrypted_credentials))


def dump_credentials(credentials: dict) -> str:
    return encrypt_secret(json.dumps(credentials))


async def get_channel_settings(
    db: AsyncSession, tenant_id: uuid.UUID, channel: str
) -> ChannelSettings | None:
    return (
        await db.execute(
            select(ChannelSettings).where(
                ChannelSettings.tenant_id == tenant_id, ChannelSettings.channel == channel
            )
        )
    ).scalar_one_or_none()


async def get_settings_by_public_key(
    db: AsyncSession, public_key: str, channel: str
) -> ChannelSettings | None:
    """The public (unauthenticated) entry point's tenant resolution. Returns
    None for an unknown key, a different channel's key, or a disabled channel."""
    row = (
        await db.execute(select(ChannelSettings).where(ChannelSettings.public_key == public_key))
    ).scalar_one_or_none()
    if row is None or row.channel != channel or not row.enabled:
        return None
    return row


async def resolve_agent(
    db: AsyncSession, tenant_id: uuid.UUID, agent_id: uuid.UUID | None
) -> AIAgent | None:
    if agent_id is None:
        return None
    agent = await db.get(AIAgent, agent_id)
    if agent is None or agent.tenant_id != tenant_id:
        return None
    return agent


# ---------------------------------------------------------------- rate limit


async def hit_rate_limit(bucket: str, *, limit: int, window_seconds: int = 60) -> bool:
    """True when `bucket` has exceeded `limit` hits in the current window.
    Fails open if Redis is unreachable (same policy as app/core/rate_limit.py)."""
    if limit <= 0:
        return False
    window = int(time.time()) // window_seconds
    key = f"rl:channels:{bucket}:{window}"
    try:
        client = _redis()
        count = await client.incr(key)
        if count == 1:
            await client.expire(key, window_seconds + 5)
    except (RedisError, OSError):
        logger.warning("channel rate limiter unavailable; allowing request", exc_info=True)
        return False
    return count > limit


# ---------------------------------------------------------------- customers / threads


async def find_or_create_customer_by_email(
    db: AsyncSession, tenant_id: uuid.UUID, email: str, name: str | None = None
) -> tuple[Customer, bool]:
    email = email.strip().lower()
    customer = (
        await db.execute(
            select(Customer).where(
                Customer.tenant_id == tenant_id, func.lower(Customer.email) == email
            )
        )
    ).scalars().first()
    if customer is not None:
        return customer, False
    customer = Customer(tenant_id=tenant_id, name=(name or email)[:255], email=email)
    db.add(customer)
    await db.flush()
    return customer, True


async def get_thread(db: AsyncSession, conversation_id: uuid.UUID) -> ChannelThread | None:
    return (
        await db.execute(
            select(ChannelThread).where(ChannelThread.conversation_id == conversation_id)
        )
    ).scalar_one_or_none()


async def find_thread(
    db: AsyncSession, tenant_id: uuid.UUID, channel: ConversationChannel, external_id: str
) -> ChannelThread | None:
    return (
        await db.execute(
            select(ChannelThread).where(
                ChannelThread.tenant_id == tenant_id,
                ChannelThread.channel == channel.value,
                ChannelThread.external_id == external_id,
            )
        )
    ).scalar_one_or_none()


async def ensure_thread(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    channel: ConversationChannel,
    external_id: str,
    *,
    customer_id: uuid.UUID | None,
    contact_address: str | None = None,
    via_address: str | None = None,
    subject: str | None = None,
) -> tuple[Conversation, ChannelThread, bool]:
    """The conversation for (tenant, channel, external_id), created on first
    contact. Returns (conversation, thread, created)."""
    thread = await find_thread(db, tenant_id, channel, external_id)
    if thread is not None:
        conversation = await db.get(Conversation, thread.conversation_id)
        if conversation is not None:
            if customer_id and conversation.customer_id is None:
                conversation.customer_id = customer_id
            return conversation, thread, False

    conversation = Conversation(tenant_id=tenant_id, customer_id=customer_id, channel=channel)
    db.add(conversation)
    await db.flush()
    thread = ChannelThread(
        tenant_id=tenant_id,
        conversation_id=conversation.id,
        channel=channel.value,
        external_id=external_id,
        contact_address=contact_address,
        via_address=via_address,
        subject=subject[:255] if subject else None,
    )
    db.add(thread)
    await db.flush()
    return conversation, thread, True


async def message_already_seen(
    db: AsyncSession, tenant_id: uuid.UUID, provider_message_id: str | None
) -> bool:
    """Providers retry webhooks; an inbound message id we already stored is a replay."""
    if not provider_message_id:
        return False
    found = (
        await db.execute(
            select(Message.id)
            .where(
                Message.tenant_id == tenant_id,
                Message.provider_message_id == provider_message_id[:128],
                Message.direction == MessageDirection.INBOUND,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return found is not None


async def find_email_conversation_id(
    db: AsyncSession, tenant_id: uuid.UUID, message_ids: list[str]
) -> uuid.UUID | None:
    """Email threading: the conversation owning any Message-ID the reply
    refers to via In-Reply-To / References."""
    ids = [m[:128] for m in message_ids if m]
    if not ids:
        return None
    return (
        await db.execute(
            select(Message.conversation_id)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Message.tenant_id == tenant_id,
                Conversation.channel == ConversationChannel.EMAIL,
                Message.provider_message_id.in_(ids),
            )
            .limit(1)
        )
    ).scalar_one_or_none()


async def record_message(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    *,
    direction: MessageDirection,
    sender: MessageSender,
    text: str,
    provider_message_id: str | None = None,
    media_url: str | None = None,
) -> Message:
    message = Message(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        direction=direction,
        sender=sender,
        text=text,
        media_url=media_url[:512] if media_url else None,
        provider_message_id=provider_message_id[:128] if provider_message_id else None,
        created_at=datetime.now(UTC),
    )
    db.add(message)
    await db.flush()
    return message


# ---------------------------------------------------------------- human takeover

# Staff replying in the inbox takes the conversation over; the AI comes back on
# "hand back to AI", or by itself once staff have been silent this long.
TAKEOVER_DURATION = timedelta(hours=12)


def ai_paused(conversation: Conversation, now: datetime | None = None) -> bool:
    until = conversation.ai_paused_until
    return until is not None and until > (now or datetime.now(UTC))


def pause_ai(conversation: Conversation, now: datetime | None = None) -> None:
    conversation.ai_paused_until = (now or datetime.now(UTC)) + TAKEOVER_DURATION


def resume_ai(conversation: Conversation) -> None:
    conversation.ai_paused_until = None


# ---------------------------------------------------------------- inbound pipeline


@dataclass
class InboundOutcome:
    conversation: Conversation
    reply_text: str | None  # None when no AI agent is attached (staff answers from the inbox)
    reply_message_id: uuid.UUID | None


async def _escalation_ticket(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    customer_id: uuid.UUID | None,
    channel: ConversationChannel,
    reason: str | None,
) -> Ticket | None:
    if customer_id is None:
        return None  # tickets require a customer; an anonymous web visitor has none
    ticket = Ticket(
        tenant_id=tenant_id,
        customer_id=customer_id,
        subject=f"{channel.value} conversation needs a human agent: {reason or 'requested by AI agent'}"[
            :255
        ],
        priority=TicketPriority.URGENT,
    )
    db.add(ticket)
    await db.flush()
    from app.services.operations import apply_sla_safely

    await apply_sla_safely(db, tenant_id, ticket)
    return ticket


def _fire_inbound_events(
    *,
    tenant_id: uuid.UUID,
    conversation: Conversation,
    channel: ConversationChannel,
    text: str,
    ticket: Ticket | None,
    customer_created: bool,
    source: str,
) -> None:
    customer_id = str(conversation.customer_id) if conversation.customer_id else None
    if customer_created and customer_id:
        enqueue_workflow_trigger(
            tenant_id=tenant_id,
            trigger=WorkflowTrigger.CUSTOMER_CREATED,
            payload={"customer_id": customer_id, "source": source},
        )
    data = {
        "conversation_id": str(conversation.id),
        "customer_id": customer_id,
        "channel": channel.value,
        "text": text,
    }
    enqueue_workflow_trigger(
        tenant_id=tenant_id, trigger=WorkflowTrigger.MESSAGE_RECEIVED, payload=data
    )
    enqueue_webhook_event(tenant_id=tenant_id, event="message.received", data=data)
    if ticket is not None:
        ticket_data = {
            "ticket_id": str(ticket.id),
            "customer_id": customer_id,
            "priority": TicketPriority.URGENT.value,
            "source": f"{source}_escalation",
        }
        enqueue_webhook_event(tenant_id=tenant_id, event="ticket.created", data=ticket_data)
        enqueue_workflow_trigger(
            tenant_id=tenant_id, trigger=WorkflowTrigger.TICKET_CREATED, payload=ticket_data
        )


async def process_inbound_text(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent: AIAgent | None,
    conversation: Conversation,
    text: str,
    provider_message_id: str | None,
    customer_created: bool = False,
    source: str,
    media_url: str | None = None,
    chat_options: dict | None = None,
) -> InboundOutcome:
    """Stores the customer's message, lets the AI agent answer (when one is
    attached) and commits. Sending the reply is the caller's job so each
    channel can report delivery honestly."""
    channel = conversation.channel
    ticket: Ticket | None = None
    reply_text: str | None = None
    reply_message_id: uuid.UUID | None = None

    # A "4" in answer to our CSAT survey is feedback, not a question for the AI.
    thanks = await csat.capture_reply(
        db,
        tenant_id=tenant_id,
        customer_id=conversation.customer_id,
        channel=channel,
        text=text,
    )
    if thanks is not None:
        await record_message(
            db,
            tenant_id,
            conversation.id,
            direction=MessageDirection.INBOUND,
            sender=MessageSender.CUSTOMER,
            text=text,
            provider_message_id=provider_message_id,
            media_url=media_url,
        )
        reply = await record_message(
            db,
            tenant_id,
            conversation.id,
            direction=MessageDirection.OUTBOUND,
            sender=MessageSender.AI_AGENT,
            text=thanks,
        )
        await db.commit()
        return InboundOutcome(conversation, thanks, reply.id)

    if agent is not None and ai_paused(conversation):
        agent = None  # staff have taken over this conversation

    extra_system = ""
    cited: list[dict] = []
    if agent is not None and source == "web_chat" and (chat_options or {}).get("use_knowledge") is not False:
        extra_system, canned_reply, cited = await _knowledge_for_chat(db, tenant_id, text, chat_options or {})
        if canned_reply is not None:
            await record_message(
                db,
                tenant_id,
                conversation.id,
                direction=MessageDirection.INBOUND,
                sender=MessageSender.CUSTOMER,
                text=text,
                provider_message_id=provider_message_id,
                media_url=media_url,
            )
            reply = await record_message(
                db,
                tenant_id,
                conversation.id,
                direction=MessageDirection.OUTBOUND,
                sender=MessageSender.AI_AGENT,
                text=canned_reply,
            )
            await db.commit()
            return InboundOutcome(conversation, canned_reply, reply.id)

    if agent is not None and source == "web_chat":
        extra_system += str((chat_options or {}).get("_visitor_context") or "")

    if agent is not None:
        result = await run_turn(
            db=db,
            tenant_id=tenant_id,
            agent=agent,
            conversation=conversation,
            customer_text=text,
            extra_system=extra_system,
        )
        reply_text, reply_message_id = result.reply_text, result.reply_message_id
        if cited and reply_message_id and not result.transfer_requested:
            reply_row = await db.get(Message, reply_message_id)
            if reply_row is not None:
                reply_row.sources = cited
        if result.transfer_requested:
            ticket = await _escalation_ticket(
                db, tenant_id, conversation.customer_id, channel, result.transfer_reason
            )
        if provider_message_id:
            last_inbound = (
                await db.execute(
                    select(Message)
                    .where(
                        Message.conversation_id == conversation.id,
                        Message.direction == MessageDirection.INBOUND,
                    )
                    .order_by(Message.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if last_inbound is not None:
                last_inbound.provider_message_id = provider_message_id[:128]
                if media_url:
                    last_inbound.media_url = media_url[:512]
    else:
        await record_message(
            db,
            tenant_id,
            conversation.id,
            direction=MessageDirection.INBOUND,
            sender=MessageSender.CUSTOMER,
            text=text,
            provider_message_id=provider_message_id,
            media_url=media_url,
        )

    await db.commit()
    _fire_inbound_events(
        tenant_id=tenant_id,
        conversation=conversation,
        channel=channel,
        text=text,
        ticket=ticket,
        customer_created=customer_created,
        source=source,
    )
    return InboundOutcome(conversation, reply_text, reply_message_id)


# ---------------------------------------------------------------- outbound routing


def _email_subject(thread: ChannelThread | None) -> str:
    subject = (thread.subject if thread and thread.subject else "Your conversation").strip()
    return subject if subject.lower().startswith("re:") else f"Re: {subject}"


async def _email_threading(db: AsyncSession, conversation_id: uuid.UUID) -> tuple[str | None, list[str]]:
    rows = (
        await db.execute(
            select(Message.provider_message_id)
            .where(
                Message.conversation_id == conversation_id,
                Message.provider_message_id.is_not(None),
            )
            .order_by(Message.created_at.desc())
            .limit(10)
        )
    ).scalars().all()
    ids = [r for r in reversed(rows) if r]
    return (ids[-1] if ids else None), ids[:-1] if ids else []


async def deliver_reply(
    db: AsyncSession, conversation: Conversation, text: str
) -> str | None:
    """Sends `text` to the customer over the conversation's own channel and
    returns the provider message id (None where the channel has none, e.g. web
    chat, which the visitor's widget polls). Raises ChannelSendError."""
    tenant_id = conversation.tenant_id
    channel = conversation.channel
    thread = await get_thread(db, conversation.id)
    customer = await db.get(Customer, conversation.customer_id) if conversation.customer_id else None

    if channel == ConversationChannel.WEB_CHAT:
        return None

    if channel == ConversationChannel.WHATSAPP:
        accounts = (
            await db.execute(select(WhatsAppAccount).where(WhatsAppAccount.tenant_id == tenant_id))
        ).scalars().all()
        # Reply from the business number the customer wrote to.
        account = next((a for a in accounts if thread and a.phone_number_id == thread.via_address), None) or (
            accounts[0] if accounts else None
        )
        if account is None:
            raise ChannelSendError("No WhatsApp number is connected")
        to = (thread.contact_address if thread else None) or (customer.phone if customer else None)
        if not to:
            raise ChannelSendError("The customer has no phone number")
        try:
            await plans.ensure_whatsapp_available(db, tenant_id)
            # The caller saves this reply as a Message, which is what gets counted.
            result = await send_text_message(account, to=to, body=text, metered=False)
        except plans.PlanLimitError as exc:
            raise ChannelSendError(exc.message) from exc
        except WhatsAppAPIError as exc:
            raise ChannelSendError(str(exc)) from exc
        messages = result.get("messages") or [{}]
        return messages[0].get("id")

    if channel == ConversationChannel.SMS:
        to = (thread.contact_address if thread else None) or (customer.phone if customer else None)
        sms = await get_channel_settings(db, tenant_id, "sms")
        from_ = (thread.via_address if thread else None) or ((sms.config or {}).get("from_number") if sms else None)
        if not to or not from_:
            raise ChannelSendError("SMS needs a customer phone number and a sending number")
        return await send_sms_message(db, tenant_id, to=to, from_=from_, body=text)

    if channel in (ConversationChannel.EMAIL, ConversationChannel.WEB_FORM):
        to = (thread.contact_address if thread else None) or (customer.email if customer else None)
        if not to:
            raise ChannelSendError("The customer has no email address")
        email_settings = await get_channel_settings(db, tenant_id, "email")
        in_reply_to, references = await _email_threading(db, conversation.id)
        return await send_email_message(
            to_email=to,
            subject=_email_subject(thread),
            body=text,
            in_reply_to=in_reply_to,
            references=references,
            reply_to=(email_settings.config or {}).get("reply_to_address") if email_settings else None,
        )

    if channel in (ConversationChannel.INSTAGRAM, ConversationChannel.MESSENGER):
        meta = await get_channel_settings(db, tenant_id, channel.value)
        if meta is None or thread is None or not thread.contact_address:
            raise ChannelSendError(f"{channel.value} is not configured for this conversation")
        token = load_credentials(meta).get("page_access_token", "")
        return await send_meta_message(
            page_access_token=token,
            recipient_id=thread.contact_address,
            text=text,
            channel=channel.value,
        )

    raise ChannelSendError(f"Replies are not supported on the {channel.value} channel")


DEFAULT_KB_FALLBACK = "I don't have that information yet. Please leave your details and a team member will get back to you."


async def knowledge_hits_for_visitors(db: AsyncSession, tenant_id: uuid.UUID, text: str, limit: int = 4) -> list[dict]:
    """Knowledge-base matches a website visitor may be answered from (internal-only documents are left out)."""
    hits = await knowledge_search(db, tenant_id=tenant_id, query=text, limit=limit)
    return [h for h in hits if not h["internal_only"]]


async def _knowledge_for_chat(
    db: AsyncSession, tenant_id: uuid.UUID, text: str, options: dict
) -> tuple[str, str | None, list[dict]]:
    """Looks the visitor's question up in the business's knowledge base.

    Returns (extra instructions for the AI, a ready-made reply to send instead of asking the AI,
    the passages the answer will draw on, for citing).
    The reply is only set in "answer only from my knowledge base" mode when nothing matched."""
    hits = await knowledge_hits_for_visitors(db, tenant_id, text)
    strict = bool(options.get("kb_only"))
    if not hits:
        if strict:
            return "", (str(options.get("fallback_message") or "").strip()[:300] or DEFAULT_KB_FALLBACK), []
        return "", None, []
    excerpts = "\n---\n".join(f"[{h['title']}] {h['content'][:900]}" for h in hits)
    note = (
        "\n\nKnowledge base excerpts that match the customer's latest message. Answer from them; "
        "quote prices, hours and policies only as written here.\n" + excerpts
    )
    if strict:
        note += (
            "\nAnswer ONLY from these excerpts. If they do not cover the question, say you do not know "
            "and offer to connect a team member."
        )
    return note, None, knowledge_citations(hits)


async def send_ai_reply(db: AsyncSession, outcome: InboundOutcome) -> dict:
    """Delivers the AI's reply, tags the stored message with the provider id
    (so email threading works) and reports whether it actually went out."""
    if outcome.reply_text is None:
        return {"replied": False, "error": None}
    try:
        provider_id = await deliver_reply(db, outcome.conversation, outcome.reply_text)
    except ChannelSendError as exc:
        logger.warning("AI reply not delivered (%s): %s", outcome.conversation.channel.value, exc)
        if outcome.reply_message_id:  # show it as failed in the inbox, with the reason
            message = await db.get(Message, outcome.reply_message_id)
            if message is not None:
                message.delivery_status = "failed"
                message.delivery_error = str(exc)[:1000]
                await db.commit()
        return {"replied": False, "error": str(exc)}
    if provider_id and outcome.reply_message_id:
        message = await db.get(Message, outcome.reply_message_id)
        if message is not None:
            message.provider_message_id = provider_id[:128]
            await db.commit()
    return {"replied": True, "error": None}


# ---------------------------------------------------------------- form spam guard

_LINK = re.compile(r"https?://|www\.", re.IGNORECASE)
_MAX_FIELD_LENGTHS = {"name": 120, "email": 254, "phone": 32, "subject": 200, "message": 5000}


def evaluate_form_spam(fields: dict, *, elapsed_seconds: float | None = None) -> str | None:
    """Returns the reason a web-form submission looks like spam, or None.

    Checks: honeypot (`website` / `hp` must be empty — humans never see it),
    submitted faster than a human could fill it (`elapsed_seconds`), oversize
    fields, too many links, and a missing/short message."""
    if str(fields.get("website") or fields.get("hp") or "").strip():
        return "honeypot field filled"
    if elapsed_seconds is not None and elapsed_seconds < 2.0:
        return "submitted too quickly"
    for name, limit in _MAX_FIELD_LENGTHS.items():
        if len(str(fields.get(name) or "")) > limit:
            return f"{name} too long"
    message = str(fields.get("message") or "")
    if len(message.strip()) < 3:
        return "message too short"
    if len(_LINK.findall(message)) > 2:
        return "too many links"
    return None


# ---------------------------------------------------------------- unified inbox

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _inbox_columns():
    last_msg = (
        select(Message.conversation_id, func.max(Message.created_at).label("last_at"))
        .group_by(Message.conversation_id)
        .subquery()
    )
    last_out = (
        select(Message.conversation_id, func.max(Message.created_at).label("last_out"))
        .where(Message.direction == MessageDirection.OUTBOUND)
        .group_by(Message.conversation_id)
        .subquery()
    )
    cutoff = case(
        (ChannelThread.id.is_(None), func.coalesce(last_out.c.last_out, _EPOCH)),
        else_=func.coalesce(ChannelThread.last_read_at, _EPOCH),
    )
    unread = (
        select(Message.conversation_id, func.count().label("n"))
        .select_from(Message)
        .outerjoin(ChannelThread, ChannelThread.conversation_id == Message.conversation_id)
        .outerjoin(last_out, last_out.c.conversation_id == Message.conversation_id)
        .where(Message.direction == MessageDirection.INBOUND, Message.created_at > cutoff)
        .group_by(Message.conversation_id)
        .subquery()
    )
    return last_msg, unread


async def list_inbox(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    channel: str | None = None,
    unread_only: bool = False,
    search: str | None = None,
    conversation_id: uuid.UUID | None = None,
    limit: int = 30,
    offset: int = 0,
) -> list[dict]:
    """Conversations of every channel (the internal staff assistant excluded)
    newest activity first, with the last message, unread count and customer."""
    last_msg, unread = _inbox_columns()
    activity = func.coalesce(last_msg.c.last_at, Conversation.created_at)
    unread_n = func.coalesce(unread.c.n, 0)

    query = (
        select(Conversation, Customer, ChannelThread, activity.label("activity"), unread_n.label("unread"))
        .outerjoin(Customer, Customer.id == Conversation.customer_id)
        .outerjoin(ChannelThread, ChannelThread.conversation_id == Conversation.id)
        .outerjoin(last_msg, last_msg.c.conversation_id == Conversation.id)
        .outerjoin(unread, unread.c.conversation_id == Conversation.id)
        .where(
            Conversation.tenant_id == tenant_id,
            Conversation.channel != ConversationChannel.INTERNAL,
        )
    )
    if conversation_id:
        query = query.where(Conversation.id == conversation_id)
    if channel:
        query = query.where(Conversation.channel == ConversationChannel(channel))
    if unread_only:
        query = query.where(unread_n > 0, Conversation.channel != ConversationChannel.VOICE)
    if search:
        like = f"%{search.strip()}%"
        query = query.where(
            or_(
                Customer.name.ilike(like),
                Customer.phone.ilike(like),
                Customer.email.ilike(like),
                ChannelThread.subject.ilike(like),
                ChannelThread.contact_address.ilike(like),
            )
        )
    rows = (
        await db.execute(query.order_by(activity.desc(), Conversation.id).limit(limit).offset(offset))
    ).all()

    ids = [row[0].id for row in rows]
    last_messages: dict[uuid.UUID, Message] = {}
    if ids:
        latest = (
            await db.execute(
                select(Message)
                .where(Message.conversation_id.in_(ids))
                .distinct(Message.conversation_id)
                .order_by(Message.conversation_id, Message.created_at.desc())
            )
        ).scalars().all()
        last_messages = {m.conversation_id: m for m in latest}

    items = []
    for conversation, customer, thread, last_at, unread_count in rows:
        last = last_messages.get(conversation.id)
        items.append(
            {
                "id": conversation.id,
                "channel": conversation.channel.value,
                "customer": (
                    {
                        "id": customer.id,
                        "name": customer.name,
                        "phone": customer.phone,
                        "email": customer.email,
                    }
                    if customer
                    else None
                ),
                "subject": thread.subject if thread else None,
                "contact_address": thread.contact_address if thread else None,
                "last_message": (
                    {
                        "text": last.text,
                        "direction": last.direction.value,
                        "sender": last.sender.value,
                        "created_at": last.created_at,
                    }
                    if last
                    else None
                ),
                "last_message_at": last_at,
                "unread_count": 0 if conversation.channel == ConversationChannel.VOICE else int(unread_count),
                "created_at": conversation.created_at,
            }
        )
    return items


async def mark_conversation_read(db: AsyncSession, conversation: Conversation) -> None:
    thread = await get_thread(db, conversation.id)
    if thread is None:
        thread = ChannelThread(
            tenant_id=conversation.tenant_id,
            conversation_id=conversation.id,
            channel=conversation.channel.value,
            external_id=f"conversation:{conversation.id}",
        )
        db.add(thread)
    thread.last_read_at = datetime.now(UTC)
    await db.flush()


def webhook_urls(public_key_by_channel: dict[str, str | None]) -> dict[str, str]:
    base = f"{get_settings().public_base_url}/api/v1/channels"
    urls = {
        "sms": f"{base}/sms/webhook",
        "meta": f"{base}/meta/webhook",
        "voicemail": f"{base}/voicemail/recording",
    }
    if public_key_by_channel.get("email"):
        urls["email"] = f"{base}/email/webhook/{public_key_by_channel['email']}"
    if public_key_by_channel.get("web_form"):
        urls["web_form"] = f"{base}/forms/{public_key_by_channel['web_form']}"
    return urls

