"""Exotel, an Indian phone carrier, as a second way for a workspace to take and make calls.

A workspace saves its own Exotel API key and token (like its own Twilio account). We give it a
private stream address to put in its Exotel flow; when a call arrives, Exotel streams the audio
to our realtime gateway, which asks `accept_call` below who owns it. The address's secret is what
ties a call to a workspace; it is stored only as a hash, so it can be shown once and replaced.

Outbound calls use the same flow: Exotel rings the customer from the workspace's ExoPhone and, when
they answer, runs the flow (App ID saved as `outbound_app_id`), whose Voicebot step streams the call
to the gateway like an incoming one.
"""

import hashlib
import hmac
import json
import re
import secrets
import uuid

import httpx
from app.core.config import get_settings
from app.core.crypto import decrypt_secret
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import (
    Call,
    Integration,
    IntegrationCredential,
    IntegrationStatus,
    PhoneNumber,
)

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


def to_exotel_local(e164: str) -> str:
    """+912241491386 -> 02241491386: how Exotel lists an Indian ExoPhone, and the CallerId
    it expects."""
    if e164.startswith("+91") and len(e164) == 13:
        return "0" + e164[3:]
    return e164


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
    if response.status_code in (400, 404):
        # Exotel answers 400 for an Account SID it has never heard of, or one in the other region.
        raise ExotelError("Exotel does not recognise this Account SID. Check the SID and the region.")
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
    previous = await _integration(db, tenant_id)
    if previous is not None and previous.config.get("outbound_app_id"):
        config["outbound_app_id"] = previous.config["outbound_app_id"]
    secret = json.dumps({"api_key": api_key, "api_token": api_token})

    from app.api.v1.integrations import _upsert_integration  # same upsert the other providers use

    integration = await _upsert_integration(
        db, tenant_id=tenant_id, provider=PROVIDER, config=config, secret=secret
    )
    return integration, key


async def _integration(db: AsyncSession, tenant_id: uuid.UUID) -> Integration | None:
    result = await db.execute(
        select(Integration).where(
            Integration.tenant_id == tenant_id, Integration.provider == PROVIDER
        )
    )
    return result.scalar_one_or_none()


async def regenerate_stream_key(db: AsyncSession, tenant_id: uuid.UUID) -> str:
    integration = await _integration(db, tenant_id)
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
        "outbound_app_id": integration.config.get("outbound_app_id"),
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


# ------------------------------------------------------------------ outbound calls


async def set_outbound_app_id(db: AsyncSession, tenant_id: uuid.UUID, app_id: str) -> Integration:
    """Which Exotel flow (App ID, e.g. 1367464) runs when an outbound call is answered."""
    integration = await _integration(db, tenant_id)
    if integration is None:
        raise ExotelError("Connect your Exotel account first.")
    app_id = app_id.strip()
    if not app_id.isdigit():
        raise ExotelError(
            "The App ID is the number next to your flow in Exotel's App Bazaar, e.g. 1367464."
        )
    integration.config = {**integration.config, "outbound_app_id": app_id}
    return integration


async def is_exotel_number(db: AsyncSession, tenant_id: uuid.UUID, e164: str | None) -> bool:
    """Calls placed from one of the workspace's Exotel numbers go out through Exotel, not Twilio."""
    if not e164:
        return False
    result = await db.execute(
        select(PhoneNumber.id).where(
            PhoneNumber.tenant_id == tenant_id,
            PhoneNumber.e164_number == e164,
            PhoneNumber.provider == PROVIDER,
        )
    )
    return result.first() is not None


def _status_token(call_id: uuid.UUID) -> str:
    # Exotel does not sign its callbacks, so the URL carries proof that we issued it for this call.
    key = get_settings().jwt_secret_key.encode()
    return hmac.new(key, f"exotel-status:{call_id}".encode(), hashlib.sha256).hexdigest()[:32]


def status_token_ok(call_id: uuid.UUID, token: str) -> bool:
    return hmac.compare_digest(_status_token(call_id), token or "")


def status_callback_url(call_id: uuid.UUID) -> str:
    base = get_settings().public_base_url
    return (
        f"{base}/api/v1/webhooks/telephony/exotel/status"
        f"?call_id={call_id}&t={_status_token(call_id)}"
    )


def _exotel_error_message(response: httpx.Response) -> str:
    try:
        message = response.json().get("RestException", {}).get("Message") or ""
    except ValueError:
        message = ""
    if response.status_code in (401, 403) and not message:
        return (
            "Exotel did not accept the saved API key and token. "
            "Connect Exotel again under Integrations."
        )
    if message:
        return f"Exotel refused the call: {message}"
    return f"Exotel refused the call ({response.status_code}). Please try again."


async def _post(url: str, data: dict, *, auth: tuple[str, str]) -> httpx.Response:
    async with httpx.AsyncClient(timeout=15) as client:
        return await client.post(url, data=data, auth=auth)


async def place_outbound_call(db: AsyncSession, call: Call) -> str:
    """Has Exotel ring call.to_number from the ExoPhone call.from_number; returns Exotel's CallSid.

    When the customer answers, Exotel runs the workspace's flow, whose Voicebot step streams the
    call to the gateway; /internal/exotel/start then finds this call by its CallSid."""
    creds = await get_credentials(db, call.tenant_id)
    if creds is None:
        raise ExotelError(
            "Connect your Exotel account under Integrations to call from this number."
        )
    app_id = creds.get("outbound_app_id")
    if not app_id:
        raise ExotelError(
            "To call out from an Exotel number, add your Exotel flow's App ID under "
            "Integrations → Exotel (App Bazaar lists it next to the flow)."
        )
    account_sid = creds["account_sid"]
    url = f"https://{creds['subdomain']}/v1/Accounts/{account_sid}/Calls/connect.json"
    data = {
        "From": call.to_number,  # the customer: Exotel rings this leg first
        "CallerId": to_exotel_local(call.from_number or ""),
        "Url": f"http://my.exotel.com/{account_sid}/exoml/start_voice/{app_id}",
        "CallType": "trans",
        "StatusCallback": status_callback_url(call.id),
        "StatusCallbackEvents[0]": "terminal",
        "StatusCallbackContentType": "application/json",
        "CustomField": str(call.id),
    }
    try:
        response = await _post(url, data, auth=(creds["api_key"], creds["api_token"]))
    except httpx.HTTPError as exc:
        raise ExotelError("We couldn't reach Exotel. Please try again in a moment.") from exc
    if response.status_code >= 400:
        raise ExotelError(_exotel_error_message(response))
    try:
        return response.json()["Call"]["Sid"]
    except (ValueError, KeyError, TypeError) as exc:
        raise ExotelError("Exotel accepted the call but did not say which call it was.") from exc
