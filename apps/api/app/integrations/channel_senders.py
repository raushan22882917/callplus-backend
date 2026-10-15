"""Outbound delivery for each channel. Every sender raises ChannelSendError
with a user-presentable reason when the channel is not configured or the
provider rejects the message — callers must never report a message as sent
when it was not."""

import uuid

import httpx
from app.core.config import get_settings
from app.integrations.channel_payloads import chunk_text
from app.integrations.credentials import get_twilio_credentials
from app.integrations.email_client import send_email
from app.integrations.twilio_client import TwilioClientError, send_sms
from sqlalchemy.ext.asyncio import AsyncSession
from twilio.base.exceptions import TwilioException


class ChannelSendError(Exception):
    pass


async def send_sms_message(db: AsyncSession, tenant_id: uuid.UUID, *, to: str, from_: str, body: str) -> str:
    try:
        credentials = await get_twilio_credentials(db, tenant_id)
        return send_sms(to=to, from_=from_, body=body, credentials=credentials)
    except (TwilioClientError, TwilioException) as exc:
        raise ChannelSendError(f"SMS not sent: {exc}") from exc


async def send_meta_message(
    *, page_access_token: str, recipient_id: str, text: str, channel: str
) -> str | None:
    """Messenger/Instagram Send API. Returns the id of the last message sent."""
    if not page_access_token:
        raise ChannelSendError(f"{channel} page access token is not configured")
    settings = get_settings()
    url = f"https://graph.facebook.com/{settings.meta_graph_api_version}/me/messages"
    limit = 950 if channel == "instagram" else 1900
    message_id: str | None = None
    async with httpx.AsyncClient(timeout=15.0) as client:
        for part in chunk_text(text, limit):
            response = await client.post(
                url,
                headers={"Authorization": f"Bearer {page_access_token}"},
                json={
                    "recipient": {"id": recipient_id},
                    "message": {"text": part},
                    "messaging_type": "RESPONSE",
                },
            )
            if response.status_code >= 400:
                raise ChannelSendError(
                    f"{channel} send failed ({response.status_code}): {response.text[:300]}"
                )
            message_id = response.json().get("message_id")
    return message_id


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
) -> str:
    """Returns the Message-ID given to the outgoing mail."""
    settings = get_settings()
    if not settings.smtp_host:
        raise ChannelSendError("Email not sent: SMTP is not configured (SMTP_HOST is empty)")
    message_id = new_message_id()
    headers = {"Message-ID": message_id}
    if in_reply_to:
        headers["In-Reply-To"] = in_reply_to
        headers["References"] = " ".join([*references, in_reply_to][-10:])
    if reply_to:
        headers["Reply-To"] = reply_to
    escaped = (
        body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
    )
    try:
        await send_email(to_email=to_email, subject=subject, html_body=escaped, headers=headers)
    except Exception as exc:
        raise ChannelSendError(f"Email not sent: {exc}") from exc
    return message_id
