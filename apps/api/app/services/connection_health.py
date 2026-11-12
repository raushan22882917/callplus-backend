"""Is each connection real? Asks the provider, instead of trusting that details were once saved.

A saved key can be revoked, a Twilio account suspended, a WhatsApp token expired. This checks every connection a
workspace has with a cheap live call and reports one of three states:

* working: the provider accepted the credentials just now.
* failed: the provider refused them (wrong, revoked, suspended). The message says what to fix.
* unknown: we could not tell (the provider was unreachable, or has no cheap check). Never shown as connected and
  never as failed: nobody should be told a good key is bad because a provider had a slow minute.

Results are kept for five minutes per workspace so opening the page does not hit every provider each time, and
"check again" is limited to once every fifteen seconds. Each call has its own timeout and one failing check never
affects the others.
"""

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import httpx
from app.core.config import get_settings
from app.integrations import email_providers
from app.integrations import llm as llm_client
from app.integrations import sarvam as sarvam_client
from app.integrations import speech_providers
from app.integrations.credentials import _connected_integration, get_email_sender
from app.integrations.oauth.errors import OAuthError
from app.integrations.oauth.tokens import get_valid_access_token, list_connections
from app.integrations.twilio_client import TwilioCredentials, check_account
from app.integrations.whatsapp import _graph_url
from app.core.crypto import decrypt_secret
from app.services import exotel as exotel_service
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import WhatsAppAccount

logger = logging.getLogger(__name__)

CACHE_SECONDS = 300
FORCE_MIN_SECONDS = 15
CHECK_TIMEOUT = 15.0

WORKING, FAILED, UNKNOWN = "working", "failed", "unknown"
_cache: dict[uuid.UUID, tuple[float, dict]] = {}

Probe = Callable[[], Awaitable[tuple[str, str]]]


def _is_unreachable(message: str) -> bool:
    text = message.lower()
    return "could not reach" in text or "couldn't reach" in text or "try again in a moment" in text or "right now" in text


def classify(error: Exception | str) -> tuple[str, str]:
    """A provider error becomes failed (they said no) or unknown (we could not ask)."""
    message = str(error) or "The provider did not accept these details."
    return (UNKNOWN if _is_unreachable(message) else FAILED), message


async def _guard(probe: Probe) -> tuple[str, str]:
    try:
        return await asyncio.wait_for(probe(), timeout=CHECK_TIMEOUT)
    except TimeoutError:
        return UNKNOWN, "The provider took too long to answer."
    except Exception as exc:  # one broken check must not hide the others
        logger.warning("connection check crashed: %s", type(exc).__name__)
        return UNKNOWN, "We could not check this right now."


# ---- the checks -----------------------------------------------------------------------------------------


def _twilio(sid: str, token: str) -> Probe:
    async def probe() -> tuple[str, str]:
        ok, message = await asyncio.to_thread(check_account, TwilioCredentials(account_sid=sid, auth_token=token))
        if ok:
            return WORKING, message
        return classify(message)

    return probe


def _key_probe(check: Callable[[str], Awaitable[None]], key: str, errors: tuple[type[Exception], ...]) -> Probe:
    async def probe() -> tuple[str, str]:
        try:
            await check(key)
        except errors as exc:
            return classify(exc)
        return WORKING, "The provider accepted this key."

    return probe


def _email_sender(creds: "email_providers.EmailApiCredentials") -> Probe:
    async def probe() -> tuple[str, str]:
        try:
            note = await email_providers.check_sender(creds.provider, creds.api_key, creds.from_email)
        except email_providers.EmailProviderError as exc:
            return classify(exc)
        return WORKING, note or f"{email_providers.LABEL[creds.provider]} accepted this key and sender address."

    return probe


def _exotel(creds: dict) -> Probe:
    async def probe() -> tuple[str, str]:
        try:
            await exotel_service.check_credentials(
                subdomain=creds["subdomain"], account_sid=creds["account_sid"], api_key=creds["api_key"], api_token=creds["api_token"]
            )
        except exotel_service.ExotelError as exc:
            return classify(exc)
        return WORKING, "Exotel accepted these details."

    return probe


def _whatsapp(waba_id: str, phone_number_id: str, token: str) -> Probe:
    async def probe() -> tuple[str, str]:
        async with httpx.AsyncClient(timeout=12) as client:
            response = await client.get(
                _graph_url(f"{waba_id}/phone_numbers"), params={"fields": "id", "limit": 100}, headers={"Authorization": f"Bearer {token}"}
            )
        if response.status_code == 200:
            ids = {str(p.get("id")) for p in response.json().get("data", [])}
            if phone_number_id in ids:
                return WORKING, "Meta accepted this number."
            return FAILED, "This number is no longer on the WhatsApp Business account. Connect it again."
        if response.status_code in (401, 403):
            return FAILED, "Meta did not accept the access token. It may have expired or been revoked. Connect WhatsApp again."
        return UNKNOWN, "Meta did not give a clear answer right now."

    return probe


async def _bearer_get(url: str, token: str, **kwargs: Any) -> httpx.Response:
    async with httpx.AsyncClient(timeout=12) as client:
        return await client.get(url, headers={"Authorization": f"Bearer {token}"}, **kwargs)


def _oauth_probe(provider: str, token: str, extra: dict) -> Probe | None:
    """One cheap authenticated call per provider. Only a 401 or 403 means failed; anything unexpected is unknown."""

    async def run() -> tuple[str, str]:
        if provider in ("google", "gmail"):
            response = await _bearer_get("https://openidconnect.googleapis.com/v1/userinfo", token)
        elif provider == "microsoft":
            response = await _bearer_get("https://graph.microsoft.com/v1.0/me", token)
        elif provider == "slack":
            async with httpx.AsyncClient(timeout=12) as client:
                response = await client.post("https://slack.com/api/auth.test", headers={"Authorization": f"Bearer {token}"})
            if response.status_code == 200:
                body = response.json()
                if body.get("ok"):
                    return WORKING, "Slack accepted this connection."
                if body.get("error") in ("invalid_auth", "token_revoked", "account_inactive", "token_expired", "not_authed"):
                    return FAILED, "Slack no longer accepts this connection. Connect it again."
                return UNKNOWN, "Slack did not give a clear answer right now."
        elif provider == "hubspot":
            response = await _bearer_get(f"https://api.hubapi.com/oauth/v1/access-tokens/{token}", token)
        elif provider == "salesforce":
            instance = (extra.get("instance_url") or "").rstrip("/")
            if not instance:
                return UNKNOWN, "No Salesforce address on file to check."
            response = await _bearer_get(f"{instance}/services/oauth2/userinfo", token)
        elif provider == "zoho":
            domain = (extra.get("api_domain") or "").rstrip("/")
            if not domain:
                return UNKNOWN, "No Zoho address on file to check."
            response = await _bearer_get(f"{domain}/crm/v2/org", token)
        elif provider == "shopify":
            shop = extra.get("shop")
            if not shop:
                return UNKNOWN, "No store on file to check."
            async with httpx.AsyncClient(timeout=12) as client:
                response = await client.get(f"https://{shop}/admin/api/2024-10/shop.json", headers={"X-Shopify-Access-Token": token})
        else:
            return UNKNOWN, "This connection cannot be tested."
        if 200 <= response.status_code < 300:
            return WORKING, "The provider accepted this connection."
        if response.status_code in (401, 403):
            return FAILED, "The provider no longer accepts this connection. Connect it again."
        return UNKNOWN, "The provider did not give a clear answer right now."

    return run


# ---- collecting what to check ---------------------------------------------------------------------------


async def _jobs(db: AsyncSession, tenant_id: uuid.UUID) -> tuple[dict[str, Probe], dict[str, tuple[str, str]]]:
    """(probes to run, results already known). Database reads happen here, one after another; the provider calls
    run afterwards at the same time, with no session in them."""
    probes: dict[str, Probe] = {}
    known: dict[str, tuple[str, str]] = {}

    twilio = await _connected_integration(db, tenant_id, "twilio")
    if twilio is not None:
        probes["twilio"] = _twilio(twilio[0].config.get("account_sid", ""), twilio[1])

    sender = await get_email_sender(db, tenant_id)
    if sender is not None:
        probes["email_sender"] = _email_sender(sender)

    creds = await exotel_service.get_credentials(db, tenant_id)
    if creds is not None:
        probes["exotel"] = _exotel(creds)

    for provider, check, errors in (
        ("sarvam", sarvam_client.check_api_key, (sarvam_client.SarvamAPIError,)),
        ("deepgram", speech_providers.check_deepgram_key, (speech_providers.SpeechProviderError,)),
        ("elevenlabs", speech_providers.check_elevenlabs_key, (speech_providers.SpeechProviderError,)),
    ):
        own = await _connected_integration(db, tenant_id, provider)
        if own is not None:
            probes[provider] = _key_probe(check, own[1], errors)

    llm = await _connected_integration(db, tenant_id, "llm")
    if llm is not None:
        cfg = llm[0].config or {}
        if cfg.get("url") and cfg.get("model"):
            endpoint = llm_client.LlmEndpoint(url=cfg["url"], model=cfg["model"], api_key=llm[1], label=cfg.get("label") or "your language model")
            probes["llm"] = _key_probe(lambda _k: llm_client.check_endpoint(endpoint), "", (llm_client.LlmError,))

    accounts = list((await db.execute(select(WhatsAppAccount).where(WhatsAppAccount.tenant_id == tenant_id))).scalars().all())
    if accounts:
        # One answer for the page: working only if every number is, else the first problem found.
        async def whatsapp_all() -> tuple[str, str]:
            outcomes = [await _whatsapp(a.waba_id, a.phone_number_id, decrypt_secret(a.encrypted_access_token))() for a in accounts]
            for want in (FAILED, UNKNOWN):
                for state, message in outcomes:
                    if state == want:
                        return state, message
            return WORKING, "Meta accepted every number."

        probes["whatsapp"] = whatsapp_all

    for conn in await list_connections(db, tenant_id):
        key = f"oauth:{conn.provider}"
        if conn.status in ("expired", "revoked"):
            known[key] = (FAILED, conn.last_error or "This connection needs to be signed in again.")
            continue
        try:
            token = await get_valid_access_token(db, tenant_id, conn.provider)
        except OAuthError as exc:
            known[key] = (FAILED if exc.code == "reconnect_required" else UNKNOWN, str(exc))
            continue
        probe = _oauth_probe(conn.provider, token, dict(conn.extra or {}))
        if probe is not None:
            probes[key] = probe
    return probes, known


async def check_all(db: AsyncSession, tenant_id: uuid.UUID, *, force: bool = False) -> dict:
    """{"checked_at", "cached", "results": [{"id", "state", "message", "checked_at"}]} for this workspace."""
    now = time.monotonic()
    cached = _cache.get(tenant_id)
    if cached is not None:
        age = now - cached[0]
        if age < (FORCE_MIN_SECONDS if force else CACHE_SECONDS):
            return {**cached[1], "cached": True}
    probes, known = await _jobs(db, tenant_id)
    outcomes = await asyncio.gather(*(_guard(p) for p in probes.values()))
    stamp = datetime.now(UTC).isoformat()
    results = [
        {"id": key, "state": state, "message": message, "checked_at": stamp}
        for key, (state, message) in [*zip(probes.keys(), outcomes, strict=True), *known.items()]
    ]
    payload = {"checked_at": stamp, "cached": False, "results": sorted(results, key=lambda r: r["id"])}
    _cache[tenant_id] = (now, payload)
    return payload


def forget(tenant_id: uuid.UUID) -> None:
    """A connection just changed, so the next look must ask the providers again."""
    _cache.pop(tenant_id, None)
