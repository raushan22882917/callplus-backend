"""Meta WhatsApp Cloud API client (Tech Provider model — one Meta App/webhook
shared by the platform; each tenant's own phone_number_id + access token are
stored in whatsapp_accounts, encrypted). See docs.md references from research:
POST https://graph.facebook.com/{version}/{phone-number-id}/messages,
Bearer auth with the tenant's own token.
"""

import hashlib
import hmac
import logging
import uuid
from datetime import UTC, datetime

import httpx
from app.core.config import get_settings
from app.core.crypto import decrypt_secret

from packages.database.models import WhatsAppAccount
from packages.database.models.platform import UsageEvent
from packages.database.session import session_scope

logger = logging.getLogger(__name__)


class WhatsAppAPIError(Exception):
    pass


async def record_usage(tenant_id: uuid.UUID) -> None:
    """Meters one outbound WhatsApp message.

    Usage is derived from saved `Message` rows, but the AI tool, reminders,
    campaigns and CSAT send without saving one, so those sends were never counted
    and any cap or bill built on the count was too low. This writes a usage event
    in its own session, so it never disturbs the caller's transaction. Callers that
    save a Message themselves pass `metered=False` to avoid counting twice.
    """
    try:
        async with session_scope() as db:
            db.add(
                UsageEvent(
                    tenant_id=tenant_id,
                    metric="whatsapp_messages",
                    quantity=1,
                    occurred_at=datetime.now(UTC),
                    extra={"source": "send"},
                )
            )
            await db.commit()
    except Exception:  # a metering hiccup must never fail a message that was delivered
        logger.warning("failed to meter WhatsApp message for tenant %s", tenant_id, exc_info=True)


def verify_webhook_signature(raw_body: bytes, signature_header: str | None) -> bool:
    """Verify X-Hub-Signature-256 (sha256=<hex>) using the Meta app secret."""
    settings = get_settings()
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    if not settings.meta_webhook_secret:
        return False  # an unset secret would make the signature forgeable by anyone
    expected = hmac.new(
        settings.meta_webhook_secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header.removeprefix("sha256="))


async def send_template_message(
    account: WhatsAppAccount,
    *,
    to: str,
    template_name: str,
    language_code: str = "en_US",
    metered: bool = True,
) -> dict:
    """Sends an approved template with no variable substitution — enough for
    a simple campaign broadcast. Templates with parameters would need a
    `components` list built per-recipient; not needed for today's use case."""
    settings = get_settings()
    access_token = decrypt_secret(account.encrypted_access_token)
    url = (
        f"https://graph.facebook.com/{settings.meta_graph_api_version}"
        f"/{account.phone_number_id}/messages"
    )
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "template",
        "template": {"name": template_name, "language": {"code": language_code}},
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            url, headers={"Authorization": f"Bearer {access_token}"}, json=payload
        )
    if response.status_code >= 400:
        raise WhatsAppAPIError(_error_from_response(response))
    if metered:
        await record_usage(account.tenant_id)
    return response.json()


async def send_text_message(
    account: WhatsAppAccount, *, to: str, body: str, metered: bool = True
) -> dict:
    settings = get_settings()
    access_token = decrypt_secret(account.encrypted_access_token)
    url = (
        f"https://graph.facebook.com/{settings.meta_graph_api_version}"
        f"/{account.phone_number_id}/messages"
    )
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {"body": body},
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            url, headers={"Authorization": f"Bearer {access_token}"}, json=payload
        )
    if response.status_code >= 400:
        raise WhatsAppAPIError(_error_from_response(response))
    if metered:
        await record_usage(account.tenant_id)
    return response.json()


def _graph_url(path: str) -> str:
    return f"https://graph.facebook.com/{get_settings().meta_graph_api_version}/{path.lstrip('/')}"


# Meta error codes a business owner actually runs into, in plain words.
FRIENDLY_ERRORS = {
    131047: "More than 24 hours have passed since the customer's last message. Send an approved template instead.",
    131026: "The message couldn't be delivered. The number may not be on WhatsApp, or the customer hasn't accepted the latest terms.",
    131051: "WhatsApp doesn't support this kind of message.",
    131056: "Too many messages to this customer in a short time. Wait a little and try again.",
    132000: "The template's variables don't match the template. Check the template and try again.",
    132001: "This template doesn't exist (or isn't approved) in that language.",
    131049: "Meta didn't deliver this marketing message to keep the customer's experience good. Try again later.",
    190: "The WhatsApp connection has expired. Reconnect WhatsApp on the WhatsApp page.",
}


def friendly_error(code: int | None, fallback: str) -> str:
    return FRIENDLY_ERRORS.get(code or 0, fallback)


def _error_from_response(response: httpx.Response) -> str:
    try:
        err = response.json().get("error") or {}
    except ValueError:
        return response.text[:300]
    return friendly_error(err.get("code"), err.get("message") or response.text[:300])


async def download_media(account: WhatsAppAccount, media_id: str) -> tuple[bytes, str]:
    """A customer's photo / voice note / document: look up its short-lived URL,
    then fetch the bytes with the same token."""
    headers = {"Authorization": f"Bearer {decrypt_secret(account.encrypted_access_token)}"}
    async with httpx.AsyncClient(timeout=30.0) as client:
        meta = await client.get(_graph_url(media_id), headers=headers)
        if meta.status_code >= 400:
            raise WhatsAppAPIError(f"Media lookup failed: {_error_from_response(meta)}")
        info = meta.json()
        data = await client.get(info["url"], headers=headers)
        if data.status_code >= 400:
            raise WhatsAppAPIError(f"Media download failed ({data.status_code})")
    return data.content, info.get("mime_type") or data.headers.get("content-type") or "application/octet-stream"


async def mark_read(account: WhatsAppAccount, message_id: str, *, typing: bool = True) -> None:
    """Blue ticks for the customer, plus "typing…" while the reply is prepared.
    Best effort: a failure here must never block the reply."""
    headers = {"Authorization": f"Bearer {decrypt_secret(account.encrypted_access_token)}"}
    body: dict = {"messaging_product": "whatsapp", "status": "read", "message_id": message_id}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            url = _graph_url(f"{account.phone_number_id}/messages")
            response = await client.post(url, headers=headers, json={**body, "typing_indicator": {"type": "text"}} if typing else body)
            if typing and response.status_code >= 400:
                await client.post(url, headers=headers, json=body)  # older API versions: read receipt only
    except httpx.HTTPError:
        pass
