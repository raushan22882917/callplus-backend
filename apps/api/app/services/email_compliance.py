"""What a workspace must honour before it sends marketing email.

* A suppression list: addresses that unsubscribed, bounced, complained, or were added by staff.
* Signed unsubscribe links (no login) and the one-click headers Gmail and Yahoo ask bulk senders for.
* A footer carrying the business name and postal address (CAN-SPAM).
* An optional switch that limits marketing email to people with a granted marketing consent.

Transactional mail (password reset, invites, receipts) is exempt and never calls this.
"""

import base64
import hashlib
import hmac
import html
import uuid
from dataclasses import dataclass

from app.core.config import get_settings
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models.email_suppression import REASONS, EmailSuppression
from packages.database.models.privacy import ConsentRecord, PrivacyProfile

ONE_CLICK = "List-Unsubscribe=One-Click"


class ComplianceError(Exception):
    pass


def normalize_email(value: str | None) -> str | None:
    cleaned = (value or "").strip().lower()
    return cleaned if "@" in cleaned and len(cleaned) <= 255 else None


# ---- unsubscribe links ------------------------------------------------------------------------


def _key() -> bytes:
    return hmac.new(get_settings().jwt_secret_key.encode(), b"email-unsubscribe-v1", hashlib.sha256).digest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def make_token(tenant_id: uuid.UUID, email: str) -> str:
    """tenant.email.signature. It has no expiry on purpose: an old email's link must keep working."""
    body = f"{tenant_id.hex}.{_b64(email.strip().lower().encode())}"
    return f"{body}.{_b64(hmac.new(_key(), body.encode(), hashlib.sha256).digest())}"


def parse_token(token: str) -> tuple[uuid.UUID, str] | None:
    try:
        tenant_hex, email_part, signature = token.split(".")
        body = f"{tenant_hex}.{email_part}"
        expected = _b64(hmac.new(_key(), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(expected, signature):
            return None
        email = normalize_email(_unb64(email_part).decode())
        return (uuid.UUID(hex=tenant_hex), email) if email else None
    except (ValueError, UnicodeDecodeError):
        return None


def unsubscribe_url(tenant_id: uuid.UUID, email: str) -> str:
    base = get_settings().public_base_url.rstrip("/")
    return f"{base}/api/v1/email/unsubscribe/{make_token(tenant_id, email)}"


def unsubscribe_headers(url: str) -> dict[str, str]:
    """RFC 8058: a mail app can unsubscribe with one POST, no page to open."""
    return {"List-Unsubscribe": f"<{url}>", "List-Unsubscribe-Post": ONE_CLICK}


# ---- suppression list -------------------------------------------------------------------------


async def get_suppression(db: AsyncSession, tenant_id: uuid.UUID, email: str | None) -> EmailSuppression | None:
    address = normalize_email(email)
    if address is None:
        return None
    return (
        await db.execute(
            select(EmailSuppression).where(EmailSuppression.tenant_id == tenant_id, EmailSuppression.email == address)
        )
    ).scalar_one_or_none()


async def suppressed_set(db: AsyncSession, tenant_id: uuid.UUID, emails: list[str]) -> set[str]:
    addresses = {a for a in (normalize_email(e) for e in emails) if a}
    if not addresses:
        return set()
    rows = await db.execute(
        select(EmailSuppression.email).where(
            EmailSuppression.tenant_id == tenant_id, EmailSuppression.email.in_(addresses)
        )
    )
    return set(rows.scalars().all())


async def suppress(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    email: str,
    *,
    reason: str,
    source: str,
    detail: str | None = None,
) -> bool:
    """Adds the address. Returns False if it was already listed (the first reason is kept)."""
    address = normalize_email(email)
    if address is None:
        raise ComplianceError("That is not an email address.")
    if reason not in REASONS:
        raise ComplianceError(f"Unknown reason '{reason}'.")
    result = await db.execute(
        pg_insert(EmailSuppression)
        .values(tenant_id=tenant_id, email=address, reason=reason, source=source, detail=(detail or None) and detail[:500])
        .on_conflict_do_nothing(constraint="uq_email_suppression_tenant_email")
    )
    await db.flush()
    return bool(result.rowcount)


async def unsubscribe(db: AsyncSession, tenant_id: uuid.UUID, email: str, *, source: str) -> bool:
    """Someone opted out. Lists the address and takes back their marketing and call-back consent. That
    also opts the same customer out on every other channel (see privacy.withdraw_consent)."""
    from app.services import privacy

    added = await suppress(db, tenant_id, email, reason="unsubscribed", source=source)
    await privacy.withdraw_consent(db, tenant_id, email=email, purposes=["marketing", "call_back"], via=source)
    return added


# ---- consent ----------------------------------------------------------------------------------


async def has_marketing_consent(db: AsyncSession, tenant_id: uuid.UUID, email: str | None) -> bool:
    address = normalize_email(email)
    if address is None:
        return False
    return (
        await db.execute(
            select(ConsentRecord.id).where(
                ConsentRecord.tenant_id == tenant_id,
                ConsentRecord.purpose == "marketing",
                ConsentRecord.status == "granted",
                ConsentRecord.subject_email == address,
            ).limit(1)
        )
    ).first() is not None


async def profile(db: AsyncSession, tenant_id: uuid.UUID) -> PrivacyProfile:
    from app.services import privacy

    return await privacy.get_profile(db, tenant_id)


async def blocked_reason(
    db: AsyncSession, tenant_id: uuid.UUID, email: str | None, *, requires_consent: bool
) -> str | None:
    """Why a marketing email must not go to this address, or None if it may."""
    hit = await get_suppression(db, tenant_id, email)
    if hit is not None:
        return {
            "unsubscribed": "recipient unsubscribed",
            "bounced": "address bounced earlier",
            "complaint": "recipient marked an email as spam",
            "manual": "address is on the suppression list",
        }.get(hit.reason, "address is suppressed")
    if requires_consent and not await has_marketing_consent(db, tenant_id, email):
        return "no marketing consent on record"
    return None


# ---- footer -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Sender:
    name: str
    address: str


async def sender_identity(db: AsyncSession, tenant_id: uuid.UUID) -> Sender | None:
    """Business name and postal address for the footer, or None if no address is on file yet."""
    row = await profile(db, tenant_id)
    address = " ".join((row.address or "").split())
    if not address:
        return None
    return Sender(name=(row.legal_name or "").strip() or "Our team", address=address)


def footer_text(sender: Sender, url: str) -> str:
    return f"\n\n--\n{sender.name}\n{sender.address}\nUnsubscribe: {url}"


def footer_html(sender: Sender, url: str) -> str:
    return (
        '<hr><p style="color:#666;font-size:12px">'
        f"{html.escape(sender.name)}<br>{html.escape(sender.address)}<br>"
        f'<a href="{html.escape(url, quote=True)}">Unsubscribe</a></p>'
    )
