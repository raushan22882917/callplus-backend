"""After a call, reach the customer on WhatsApp, SMS or email from the same inbox, like a CRM.

A call thread cannot be replied to. This sends a new message on the channel the owner picks, using
the customer's own phone number or email, and records it in that channel's thread (found or created
the same way inbound messages are), so the customer's answer lands in the same place.
"""

import re
import uuid
from typing import Any

from app.core.config import get_settings
from app.integrations.channel_senders import ChannelSendError, send_email_message, send_sms_message
from app.integrations.whatsapp import WhatsAppAPIError, send_text_message
from app.services import channels as svc
from app.services import plans
from app.services import whatsapp_templates as tpl
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import (
    Conversation,
    ConversationChannel,
    Customer,
    Message,
    MessageDirection,
    MessageSender,
    PhoneNumber,
    WhatsAppAccount,
    WhatsAppTemplate,
)

VIA = ("whatsapp", "sms", "email")
DEFAULT_EMAIL_SUBJECT = "Following up on your call"


class FollowUpError(Exception):
    """Something the owner can understand and fix, in plain words."""


async def _customer(db: AsyncSession, conversation: Conversation) -> Customer:
    customer = (
        await db.get(Customer, conversation.customer_id) if conversation.customer_id else None
    )
    if customer is None:
        raise FollowUpError("This conversation is not linked to a customer record yet.")
    return customer


async def _whatsapp_account(
    db: AsyncSession, tenant_id: uuid.UUID, digits: str
) -> WhatsAppAccount | None:
    accounts = (
        (await db.execute(select(WhatsAppAccount).where(WhatsAppAccount.tenant_id == tenant_id)))
        .scalars()
        .all()
    )
    if not accounts:
        return None
    thread = await svc.find_thread(db, tenant_id, ConversationChannel.WHATSAPP, digits)
    if thread is not None:
        for account in accounts:
            if account.phone_number_id == thread.via_address:
                return account
    return accounts[0]


async def _sms_from(db: AsyncSession, tenant_id: uuid.UUID) -> tuple[str | None, str | None]:
    """(sending number, why not). The SMS channel must be on, so the customer's reply reaches us."""
    sms = await svc.get_channel_settings(db, tenant_id, "sms")
    if sms is None or not sms.enabled:
        return None, "Turn on SMS under Channels first, so their reply reaches you."
    from_ = (sms.config or {}).get("from_number")
    if not from_:
        number = (
            (
                await db.execute(
                    select(PhoneNumber).where(
                        PhoneNumber.tenant_id == tenant_id, PhoneNumber.provider == "twilio"
                    )
                )
            )
            .scalars()
            .first()
        )
        from_ = number.e164_number if number else None
    if not from_:
        return None, "Add a Twilio phone number first. SMS needs a number to send from."
    return from_, None


def _digits(phone: str | None) -> str:
    return re.sub(r"\D", "", phone or "")


async def options(
    db: AsyncSession, tenant_id: uuid.UUID, conversation: Conversation
) -> dict[str, Any]:
    """Which ways of reaching this customer work right now, and if not, why, in plain words."""
    customer = (
        await db.get(Customer, conversation.customer_id) if conversation.customer_id else None
    )
    result: dict[str, Any] = {
        "customer": {
            "name": customer.name if customer else None,
            "phone": customer.phone if customer else None,
            "email": customer.email if customer else None,
        }
    }
    no_customer = "This conversation is not linked to a customer record yet."

    # WhatsApp
    if customer is None:
        result["whatsapp"] = {"available": False, "reason": no_customer, "templates": []}
    elif not customer.phone:
        result["whatsapp"] = {
            "available": False,
            "reason": "No phone number on file for this customer.",
            "templates": [],
        }
    else:
        digits = _digits(customer.phone)
        account = await _whatsapp_account(db, tenant_id, digits)
        if account is None:
            result["whatsapp"] = {
                "available": False,
                "reason": "Connect WhatsApp first (WhatsApp page).",
                "templates": [],
            }
        else:
            templates = (
                (
                    await db.execute(
                        select(WhatsAppTemplate).where(
                            WhatsAppTemplate.tenant_id == tenant_id,
                            WhatsAppTemplate.status == "APPROVED",
                        )
                    )
                )
                .scalars()
                .all()
            )
            result["whatsapp"] = {
                "available": True,
                "reason": None,
                "window_open": await _window_open(db, tenant_id, digits),
                "templates": [
                    {"id": str(t.id), "name": t.name, "preview": tpl.render(t, {})[:240]}
                    for t in templates
                ],
            }

    # SMS
    if customer is None or not customer.phone:
        result["sms"] = {
            "available": False,
            "reason": no_customer
            if customer is None
            else "No phone number on file for this customer.",
        }
    else:
        from_, why = await _sms_from(db, tenant_id)
        result["sms"] = {"available": from_ is not None, "reason": why}

    # Email
    if customer is None or not customer.email:
        result["email"] = {
            "available": False,
            "reason": no_customer
            if customer is None
            else "No email address on file for this customer.",
        }
    elif not get_settings().smtp_host:
        result["email"] = {
            "available": False,
            "reason": "Email sending is not set up on this server yet.",
        }
    else:
        result["email"] = {"available": True, "reason": None}
    return result


async def _window_open(db: AsyncSession, tenant_id: uuid.UUID, digits: str) -> bool:
    """WhatsApp allows free text only within 24 hours of the customer's last message to us."""
    thread = await svc.find_thread(db, tenant_id, ConversationChannel.WHATSAPP, digits)
    if thread is None:
        return False
    last = (
        await db.execute(
            select(Message.created_at)
            .where(
                Message.conversation_id == thread.conversation_id,
                Message.direction == MessageDirection.INBOUND,
            )
            .order_by(Message.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return tpl.window_open(last)


async def send(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    conversation: Conversation,
    *,
    via: str,
    text: str = "",
    subject: str | None = None,
    template_id: uuid.UUID | None = None,
    values: dict[str, str] | None = None,
) -> tuple[Message, Conversation]:
    """Sends the message and records it in the target channel's thread.

    Returns (message, the conversation that thread belongs to).
    """
    if via not in VIA:
        raise FollowUpError("Choose WhatsApp, SMS or email.")
    customer = await _customer(db, conversation)
    text = (text or "").strip()
    if not text and not (via == "whatsapp" and template_id):
        raise FollowUpError("Write a message first.")

    if via == "whatsapp":
        message, target = await _send_whatsapp(
            db, tenant_id, customer, text, template_id, values or {}
        )
    elif via == "sms":
        message, target = await _send_sms(db, tenant_id, customer, text)
    else:
        message, target = await _send_email(db, tenant_id, customer, text, subject)

    # A person wrote to the customer: the AI stays quiet in that thread so they don't talk over it.
    svc.pause_ai(target)
    await svc.mark_conversation_read(db, target)
    return message, target


async def _send_whatsapp(db, tenant_id, customer, text, template_id, values):
    if not customer.phone:
        raise FollowUpError("No phone number on file for this customer.")
    digits = _digits(customer.phone)
    account = await _whatsapp_account(db, tenant_id, digits)
    if account is None:
        raise FollowUpError("Connect WhatsApp first (WhatsApp page).")

    try:
        await plans.ensure_whatsapp_available(db, tenant_id)
        if template_id:
            template = await db.get(WhatsAppTemplate, template_id)
            if template is None or template.tenant_id != tenant_id:
                raise FollowUpError("That template was not found.")
            from app.services.customer_context import next_appointment

            appointment = await next_appointment(db, tenant_id, customer.id)
            filled = await tpl.context_values(
                db, tenant_id, customer, appointment=appointment, extra=values
            )
            provider_id = await tpl.send_template(
                db,
                tenant_id,
                to=digits,
                template=template,
                values=filled,
                account=account,
                metered=False,
            )
            shown = tpl.render(template, filled)
        else:
            if not await _window_open(db, tenant_id, digits):
                raise FollowUpError(
                    "They have not written to you on WhatsApp in the last 24 hours, so WhatsApp "
                    "only lets you send an approved template. Choose a template instead."
                )
            result = await send_text_message(account, to=digits, body=text, metered=False)
            provider_id = ((result.get("messages") or [{}])[0]).get("id")
            shown = text
    except plans.PlanLimitError as exc:
        raise FollowUpError(exc.message) from exc
    except (tpl.TemplateError, WhatsAppAPIError) as exc:
        raise FollowUpError(str(exc)) from exc

    target, _, _ = await svc.ensure_thread(
        db,
        tenant_id,
        ConversationChannel.WHATSAPP,
        digits,
        customer_id=customer.id,
        contact_address=digits,
        via_address=account.phone_number_id,
    )
    message = await svc.record_message(
        db,
        tenant_id,
        target.id,
        direction=MessageDirection.OUTBOUND,
        sender=MessageSender.HUMAN_AGENT,
        text=shown,
        provider_message_id=provider_id,
    )
    return message, target


async def _send_sms(db, tenant_id, customer, text):
    if not customer.phone:
        raise FollowUpError("No phone number on file for this customer.")
    from_, why = await _sms_from(db, tenant_id)
    if from_ is None:
        raise FollowUpError(why or "SMS is not ready.")
    try:
        provider_id = await send_sms_message(
            db, tenant_id, to=customer.phone, from_=from_, body=text
        )
    except ChannelSendError as exc:
        raise FollowUpError(str(exc)) from exc
    target, _, _ = await svc.ensure_thread(
        db,
        tenant_id,
        ConversationChannel.SMS,
        f"{customer.phone}|{from_}",
        customer_id=customer.id,
        contact_address=customer.phone,
        via_address=from_,
    )
    message = await svc.record_message(
        db,
        tenant_id,
        target.id,
        direction=MessageDirection.OUTBOUND,
        sender=MessageSender.HUMAN_AGENT,
        text=text,
        provider_message_id=provider_id,
    )
    return message, target


async def _send_email(db, tenant_id, customer, text, subject, extra_headers=None):
    if not customer.email:
        raise FollowUpError("No email address on file for this customer.")
    email_settings = await svc.get_channel_settings(db, tenant_id, "email")
    subject = (subject or "").strip() or DEFAULT_EMAIL_SUBJECT
    try:
        message_id = await send_email_message(
            to_email=customer.email,
            subject=subject,
            body=text,
            in_reply_to=None,
            references=[],
            reply_to=(email_settings.config or {}).get("reply_to_address")
            if email_settings
            else None,
            db=db,
            tenant_id=tenant_id,
            extra_headers=extra_headers,
        )
    except ChannelSendError as exc:
        raise FollowUpError(str(exc)) from exc
    # The customer's reply carries our Message-ID, which is how it finds this thread again.
    target, _, _ = await svc.ensure_thread(
        db,
        tenant_id,
        ConversationChannel.EMAIL,
        message_id[:255],
        customer_id=customer.id,
        contact_address=customer.email,
        subject=subject,
    )
    message = await svc.record_message(
        db,
        tenant_id,
        target.id,
        direction=MessageDirection.OUTBOUND,
        sender=MessageSender.HUMAN_AGENT,
        text=text,
        provider_message_id=message_id,
    )
    return message, target
