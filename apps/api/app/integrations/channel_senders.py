"""Outbound delivery for each channel. Every sender raises ChannelSendError
with a user-presentable reason when the channel is not configured or the
provider rejects the message — callers must never report a message as sent
when it was not."""

import math
import uuid

from app.core.config import get_settings
from app.integrations.credentials import SharedAccountError, get_twilio_credentials, require_own_twilio
from app.integrations.email_client import send_email
from app.integrations.twilio_client import TwilioClientError, send_sms
from app.services import plans
from sqlalchemy.ext.asyncio import AsyncSession
from twilio.base.exceptions import TwilioException


class ChannelSendError(Exception):
    pass


def sms_segments(body: str) -> int:
    """160 characters is one segment. A longer text is billed in segments of 160."""
    return max(1, math.ceil(len(body or "") / 160))


async def send_sms_message(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    to: str,
    from_: str,
    body: str,
    meter: bool = True,
) -> str:
    """Sends one SMS. `meter` records the segments against the plan before the
    send; post-call notifications pass False because they record the segments
    themselves, with the call id attached."""
    segments = sms_segments(body)
    event = None
    try:
        if meter:
            event = await plans.reserve_sms(db, tenant_id, segments)
        else:
            await plans.ensure_sms_available(db, tenant_id, segments)
        await require_own_twilio(db, tenant_id)
        credentials = await get_twilio_credentials(db, tenant_id)
        sid = send_sms(to=to, from_=from_, body=body, credentials=credentials)
    except plans.PlanLimitError as exc:
        raise ChannelSendError(exc.message) from exc
    except (TwilioClientError, TwilioException, SharedAccountError) as exc:
        if event is not None:
            await db.delete(event)
            await db.flush()
        raise ChannelSendError(f"SMS not sent: {exc}") from exc
    if event is not None:
        await plans.sync_overage_invoice(db, tenant_id)
    return sid


def new_message_id() -> str:
    domain = get_settings().smtp_from_email.rpartition("@")[2] or "localhost"
    return f"<{uuid.uuid4().hex}@{domain}>"


async def send_email_message(
    *,
    to_email: str,
    subject: str,
    body: str,
    in_reply_to: str | None,
    references: list[str],
    reply_to: str | None,
    db: AsyncSession | None = None,
    tenant_id: uuid.UUID | None = None,
    extra_headers: dict[str, str] | None = None,
) -> str:
    """Returns the Message-ID given to the outgoing mail.

    Pass `db` and `tenant_id` for a customer email so it counts against the
    plan. Mail the platform sends on its own (verification, password reset)
    does not come through here.
    """
    if db is not None and tenant_id is not None:
        from app.services.mailbox import MailboxError, connected_mailbox, deliver

        box = await connected_mailbox(db, tenant_id)
        if box is not None:
            event = None
            try:
                event = await plans.reserve_email(db, tenant_id)
            except plans.PlanLimitError as exc:
                raise ChannelSendError(exc.message) from exc
            escaped = (
                body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
            )
            try:
                sent = await deliver(
                    db,
                    box,
                    to_email=to_email,
                    subject=subject,
                    text=body,
                    html=escaped,
                    reply_to=reply_to,
                    in_reply_to=in_reply_to,
                    references=references,
                    extra_headers=extra_headers,
                )
            except (MailboxError, Exception) as exc:
                if event is not None:
                    await db.delete(event)
                    await db.flush()
                message = str(exc) if isinstance(exc, MailboxError) else f"Email not sent: {exc}"
                raise ChannelSendError(message) from exc
            await plans.sync_overage_invoice(db, tenant_id)
            return sent

    settings = get_settings()
    own_sender = None
    if db is not None and tenant_id is not None:
        from app.integrations.credentials import get_email_sender

        own_sender = await get_email_sender(db, tenant_id)
    if own_sender is None and not settings.smtp_host and not settings.email_api_key:
        raise ChannelSendError("Email not sent: connect a mailbox or an email sending key, or set EMAIL_API_KEY / SMTP_HOST")
    event = None
    try:
        if db is not None and tenant_id is not None:
            event = await plans.reserve_email(db, tenant_id)
    except plans.PlanLimitError as exc:
        raise ChannelSendError(exc.message) from exc
    message_id = new_message_id()
    headers = {"Message-ID": message_id, **(extra_headers or {})}
    if in_reply_to:
        headers["In-Reply-To"] = in_reply_to
        headers["References"] = " ".join([*references, in_reply_to][-10:])
    if reply_to:
        headers["Reply-To"] = reply_to
    escaped = (
        body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
    )
    try:
        await send_email(
            to_email=to_email, subject=subject, html_body=escaped, headers=headers,
            tenant_id=str(tenant_id) if (own_sender is not None and tenant_id is not None) else None, credentials=own_sender,
        )
    except Exception as exc:
        if event is not None and db is not None:
            await db.delete(event)
            await db.flush()
        raise ChannelSendError(f"Email not sent: {exc}") from exc
    if event is not None and db is not None and tenant_id is not None:
        await plans.sync_overage_invoice(db, tenant_id)
    return message_id
