"""Exotel, an Indian phone carrier, as a second way for a workspace to receive calls.

A workspace saves its own Exotel API key and token (like its own Twilio account). We give it a
private stream address to put in its Exotel flow; when a call arrives, Exotel streams the audio
to our realtime gateway, which asks `accept_call` below who owns it. The address's secret is what
ties a call to a workspace; it is stored only as a hash, so it can be shown once and replaced.
"""

import hashlib
import json
import re
import secrets
import uuid

import httpx
from app.core.config import get_settings
from app.core.crypto import decrypt_secret
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import Integration, IntegrationCredential, IntegrationStatus

PROVIDER = "exotel"
# Exotel runs two regions; an account belongs to one of them (shown in its API settings).
SUBDOMAINS = ("api.exotel.com", "api.in.exotel.com")


class ExotelError(Exception):
    """Something the person connecting Exotel can fix, in plain words."""


def to_e164(raw: str, default_country: str = "91") -> str:
    """Exotel sends numbers as 09876543210, 9876543210 or +919876543210; we store +919876543210."""
    text = (raw or "").strip()
    digits = re.sub(r"\D", "", text)
    if not digits:
        return ""
    if text.startswith("+"):
        return "+" + digits
    if digits.startswith("00"):
        return "+" + digits[2:]
    if digits.startswith("0") and len(digits) == 11:
        return "+" + default_country + digits[1:]
    if len(digits) == 10:
        return "+" + default_country + digits
    return "+" + digits


def new_stream_key() -> str:
    return secrets.token_urlsafe(32)


def hash_stream_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def stream_url(key: str) -> str:
    """The address the workspace pastes into its Exotel Voicebot step."""
    base = get_settings().realtime_ws_url
    base = base[: -len("/ws/twilio")] if base.endswith("/ws/twilio") else base.rstrip("/")
    return f"{base}/ws/exotel?k={key}"


async def check_credentials(
    *, subdomain: str, account_sid: str, api_key: str, api_token: str
) -> None:
    """Asks Exotel for the account balance: cheap, and fails with 401 when the details are wrong."""
    if subdomain not in SUBDOMAINS:
        raise ExotelError(
            "Pick the region shown in your Exotel API settings (Singapore or Mumbai)."
        )
    url = f"https://{subdomain}/v1/Accounts/{account_sid}/Balance.json"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(url, auth=(api_key, api_token))
    except httpx.HTTPError as exc:
        raise ExotelError("We couldn't reach Exotel. Please try again in a moment.") from exc
    if response.status_code in (401, 403):
        raise ExotelError(
            "Exotel did not accept this API key and token. "
            "Copy them again from Exotel → API Settings."
        )
    if response.status_code == 404:
        raise ExotelError("Exotel doesn't know this Account SID. Check the SID and the region.")
    if response.status_code >= 400:
        raise ExotelError(
            f"Exotel answered with an error ({response.status_code}). Please check the details."
        )


async def connect(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    subdomain: str,
    account_sid: str,
    api_key: str,
    api_token: str,
) -> tuple[Integration, str]:
    """Saves the workspace's Exotel details and returns (integration, new stream key).

    Connecting again replaces the details and the stream key, so an old address stops working.
    """
    key = new_stream_key()
    config = {
        "account_sid": account_sid,
        "subdomain": subdomain,
        "stream_key_sha256": hash_stream_key(key),
    }
    secret = json.dumps({"api_key": api_key, "api_token": api_token})

    from app.api.v1.integrations import _upsert_integration  # same upsert the other providers use

    integration = await _upsert_integration(
        db, tenant_id=tenant_id, provider=PROVIDER, config=config, secret=secret
    )
    return integration, key


async def regenerate_stream_key(db: AsyncSession, tenant_id: uuid.UUID) -> str:
    result = await db.execute(
        select(Integration).where(
            Integration.tenant_id == tenant_id, Integration.provider == PROVIDER
        )
    )
    integration = result.scalar_one_or_none()
    if integration is None:
        raise ExotelError("Connect your Exotel account first.")
    key = new_stream_key()
    integration.config = {**integration.config, "stream_key_sha256": hash_stream_key(key)}
    return key


async def get_credentials(db: AsyncSession, tenant_id: uuid.UUID) -> dict | None:
    """This workspace's Exotel API key and token (for outbound calls), or None if not connected."""
    result = await db.execute(
        select(Integration, IntegrationCredential)
        .join(IntegrationCredential, IntegrationCredential.integration_id == Integration.id)
        .where(
            Integration.tenant_id == tenant_id,
            Integration.provider == PROVIDER,
            Integration.status == IntegrationStatus.CONNECTED,
        )
    )
    row = result.first()
    if row is None:
        return None
    integration, credential = row
    data = json.loads(decrypt_secret(credential.encrypted_secret))
    return {
        **data,
        "account_sid": integration.config["account_sid"],
        "subdomain": integration.config["subdomain"],
    }


async def tenant_for_stream_key(db: AsyncSession, stream_key: str) -> uuid.UUID | None:
    """Which workspace does this stream address belong to? None if it is unknown or switched off."""
    wanted = hash_stream_key(stream_key)
    result = await db.execute(
        select(Integration).where(
            Integration.provider == PROVIDER,
            Integration.status == IntegrationStatus.CONNECTED,
            Integration.config["stream_key_sha256"].as_string() == wanted,
        )
    )
    integration = result.scalar_one_or_none()
    return integration.tenant_id if integration else None
