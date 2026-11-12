"""Open and click tracking for campaign email.

Every link a customer can click, and a one-pixel image, are signed with the server secret. The signature is the only
thing that makes them valid, so nobody can turn the redirect into an open redirect to a site we did not put in the
email, and nobody can fill another workspace's numbers with fake events.

Opens are an estimate (some apps pre-load images, some block them); clicks are reliable. The reports say so.
"""

import base64
import hashlib
import hmac
import re
import uuid

from app.core.config import get_settings
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models.email_event import EmailEvent

_BARE_URL = re.compile(r"(https?://[^\s<>\"']+)")
_TRAILING = ".,;:!?)"
# Gmail and Yahoo load images through their own proxies for real readers, so those are not listed here.
_BOTS = ("bot", "crawler", "spider", "scanner", "prefetch", "headless", "curl/", "python-requests")


def _key() -> bytes:
    return hmac.new(get_settings().jwt_secret_key.encode(), b"email-track-v1", hashlib.sha256).digest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(body: str) -> str:
    return _b64(hmac.new(_key(), body.encode(), hashlib.sha256).digest())


def make_token(tenant_id: uuid.UUID, campaign_id: uuid.UUID, customer_id: uuid.UUID, url: str | None = None) -> str:
    """tenant.campaign.customer[.url].signature, every part url-safe."""
    parts = [tenant_id.hex, campaign_id.hex, customer_id.hex]
    if url is not None:
        parts.append(_b64(url.encode()))
    body = ".".join(parts)
    return f"{body}.{_sign(body)}"


def parse_token(token: str) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, str | None] | None:
    try:
        *parts, signature = token.split(".")
        if len(parts) not in (3, 4) or not hmac.compare_digest(_sign(".".join(parts)), signature):
            return None
        url = _unb64(parts[3]).decode() if len(parts) == 4 else None
        if url is not None and not url.lower().startswith(("http://", "https://")):
            return None
        return uuid.UUID(hex=parts[0]), uuid.UUID(hex=parts[1]), uuid.UUID(hex=parts[2]), url
    except (ValueError, UnicodeDecodeError):
        return None


def _base() -> str:
    return get_settings().public_base_url.rstrip("/")


def open_url(tenant_id: uuid.UUID, campaign_id: uuid.UUID, customer_id: uuid.UUID) -> str:
    return f"{_base()}/api/v1/email/t/o/{make_token(tenant_id, campaign_id, customer_id)}.gif"


def click_url(tenant_id: uuid.UUID, campaign_id: uuid.UUID, customer_id: uuid.UUID, url: str) -> str:
    return f"{_base()}/api/v1/email/t/c/{make_token(tenant_id, campaign_id, customer_id, url)}"


def link_and_track(escaped_html: str, tenant_id: uuid.UUID, campaign_id: uuid.UUID, customer_id: uuid.UUID) -> str:
    """Turns each web address in the (already escaped) message into a tracked link and adds the open pixel."""
    if not get_settings().email_tracking_enabled:
        return _BARE_URL.sub(lambda m: f'<a href="{m.group(1)}">{m.group(1)}</a>', escaped_html)

    def replace(match: re.Match) -> str:
        shown = match.group(1)
        trail = ""
        while shown and shown[-1] in _TRAILING:
            trail = shown[-1] + trail
            shown = shown[:-1]
        # The text was HTML-escaped, so &amp; in the address must go back to & before it is signed.
        real = shown.replace("&amp;", "&")
        return f'<a href="{click_url(tenant_id, campaign_id, customer_id, real)}">{shown}</a>{trail}'

    pixel = f'<img src="{open_url(tenant_id, campaign_id, customer_id)}" width="1" height="1" alt="" style="display:none">'
    return _BARE_URL.sub(replace, escaped_html) + pixel


def looks_automated(user_agent: str | None) -> bool:
    agent = (user_agent or "").lower()
    return any(marker in agent for marker in _BOTS)


async def record(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    campaign_id: uuid.UUID,
    customer_id: uuid.UUID,
    kind: str,
    url: str | None = None,
    user_agent: str | None = None,
) -> None:
    db.add(
        EmailEvent(
            tenant_id=tenant_id,
            campaign_id=campaign_id,
            customer_id=customer_id,
            kind=kind,
            url=url[:2000] if url else None,
            user_agent=(user_agent or "")[:255] or None,
        )
    )
