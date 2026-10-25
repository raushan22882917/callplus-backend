"""Keeping every client's phone numbers pointed at this platform's current address.

Twilio sends each call to a URL stored on the number. If the platform's address changes (a new
server, a new domain) every client's numbers keep calling the old one and their callers hear
nothing. This walks all numbers of all clients, each with that client's own Twilio account where
they connected one, and moves only the webhooks that are ours (known paths on another host).
Anything else on a number, for example an SMS address that belongs to another service, is left alone."""

import asyncio
import logging
import uuid
from urllib.parse import urlsplit, urlunsplit

from app.core.config import get_settings
from app.integrations import twilio_client
from app.integrations.credentials import get_twilio_credentials
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import PhoneNumber, Tenant

logger = logging.getLogger(__name__)

# The addresses this platform serves for Twilio, by the number setting they belong in.
OUR_PATHS = {
    "voice_url": "/api/v1/webhooks/telephony/twilio/voice",
    "status_callback": "/api/v1/webhooks/telephony/twilio/status",
    "sms_url": "/api/v1/channels/sms/webhook",
}


def retarget(field: str, current: str | None, base_url: str) -> str | None:
    """The new address for a webhook, or None to leave it as it is: it is empty, someone
    else's, or already right."""
    if not current:
        return None
    parts = urlsplit(current)
    if parts.path.rstrip("/") != OUR_PATHS[field]:
        return None  # not ours (another provider, or a path we do not serve)
    target = urlsplit(base_url)
    wanted = urlunsplit((target.scheme, target.netloc, parts.path, parts.query, ""))
    return None if wanted == current else wanted


async def sync_number_webhooks(
    db: AsyncSession, *, tenant_id: uuid.UUID | None = None, apply: bool = False
) -> list[dict]:
    """One row per number: what is set now and what would change (and, when `apply`, whether it did).
    A failure on one number or one client never stops the others."""
    base_url = get_settings().public_base_url.rstrip("/")
    stmt = select(PhoneNumber, Tenant.slug).join(Tenant, Tenant.id == PhoneNumber.tenant_id)
    if tenant_id is not None:
        stmt = stmt.where(PhoneNumber.tenant_id == tenant_id)
    rows = (await db.execute(stmt.order_by(Tenant.slug, PhoneNumber.e164_number))).all()

    report: list[dict] = []
    for number, slug in rows:
        entry = {"tenant": slug, "number": number.e164_number, "changes": {}, "status": "ok", "error": None}
        report.append(entry)
        if number.provider != "twilio":
            entry["status"] = "skipped"
            entry["error"] = f"{number.provider} number: nothing to change at Twilio"
            continue
        try:
            credentials = await get_twilio_credentials(db, number.tenant_id)
            sid = number.provider_sid
            if not sid:  # registered by hand: find it on the client's Twilio account by its number
                sid = await asyncio.to_thread(
                    twilio_client.find_number_sid, phone_number=number.e164_number, credentials=credentials
                )
                if not sid:
                    entry["status"] = "skipped"
                    entry["error"] = "not found on the connected Twilio account"
                    continue
                if apply:
                    number.provider_sid = sid
            current = await asyncio.to_thread(
                twilio_client.fetch_number_webhooks, provider_sid=sid, credentials=credentials
            )
            changes = {
                field: new
                for field in OUR_PATHS
                if (new := retarget(field, current.get(field), base_url)) is not None
            }
            entry["changes"] = {f: {"from": current[f], "to": to} for f, to in changes.items()}
            if not changes:
                entry["status"] = "already correct"
            elif apply:
                await asyncio.to_thread(
                    twilio_client.update_number_webhooks,
                    provider_sid=sid,
                    changes=changes,
                    credentials=credentials,
                )
                entry["status"] = "updated"
            else:
                entry["status"] = "would update"
        except Exception as exc:  # one client's bad credentials must not block the rest
            logger.warning("webhook sync failed for %s", number.e164_number, exc_info=True)
            entry["status"] = "failed"
            entry["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
    return report
