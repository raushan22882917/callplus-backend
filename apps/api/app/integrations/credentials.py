"""Resolves which Twilio/Sarvam credentials to use for a given tenant:
their own (if they've connected one via POST /api/v1/integrations/{provider})
or the platform's shared one. This is what makes the platform usable by many
tenants without every tenant needing their own Twilio/Sarvam account, while
still letting an enterprise tenant bring their own for billing/data-residency
reasons.

Every call site that talks to Twilio or Sarvam on behalf of a specific
tenant should go through this module rather than reading app.core.config
directly — that's what "many tenants" actually requires structurally.
"""

import uuid
from dataclasses import dataclass

from app.core.config import get_settings
from app.core.crypto import decrypt_secret
from app.integrations import managed
from app.integrations.llm import LlmEndpoint
from app.integrations.providers import DEEPGRAM, ELEVENLABS, LLM, SARVAM
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import Integration, IntegrationCredential, IntegrationStatus, Tenant


@dataclass(frozen=True)
class TwilioCredentials:
    account_sid: str
    auth_token: str


async def _connected_integration(
    db: AsyncSession, tenant_id: uuid.UUID, provider: str
) -> tuple[Integration, str] | None:
    result = await db.execute(
        select(Integration, IntegrationCredential)
        .join(IntegrationCredential, IntegrationCredential.integration_id == Integration.id)
        .where(
            Integration.tenant_id == tenant_id,
            Integration.provider == provider,
            Integration.status == IntegrationStatus.CONNECTED,
        )
    )
    row = result.first()
    if row is None:
        return None
    integration, credential = row
    return integration, decrypt_secret(credential.encrypted_secret)


async def get_email_sender(db: AsyncSession, tenant_id: uuid.UUID):
    """The workspace's own Resend or Brevo sending account, or None. Used for its customer email."""
    from app.integrations.email_providers import EmailApiCredentials

    connected = await _connected_integration(db, tenant_id, "email_sender")
    if connected is None:
        return None
    integration, key = connected
    cfg = integration.config or {}
    if not cfg.get("from_email") or cfg.get("provider") not in ("resend", "brevo"):
        return None
    return EmailApiCredentials(provider=cfg["provider"], api_key=key, from_email=cfg["from_email"], from_name=cfg.get("from_name"))


async def uses_platform_twilio(db: AsyncSession, tenant_id: uuid.UUID) -> bool:
    """True when this tenant has not connected its own Twilio account, so a
    purchase or a call would bill the platform's account."""
    return await _connected_integration(db, tenant_id, "twilio") is None


async def get_twilio_credentials(db: AsyncSession, tenant_id: uuid.UUID) -> TwilioCredentials:
    connected = await _connected_integration(db, tenant_id, "twilio")
    if connected is not None:
        integration, auth_token = connected
        return TwilioCredentials(
            account_sid=integration.config["account_sid"], auth_token=auth_token
        )

    settings = get_settings()
    return TwilioCredentials(
        account_sid=settings.twilio_account_sid, auth_token=settings.twilio_auth_token
    )


class SharedAccountError(Exception):
    """The workspace has not connected its own account, so the shared one must not be billed."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


OWN_ACCOUNT_MESSAGE = (
    "Connect your own account under Integrations. For an Indian number, connect Exotel and "
    "add the ExoPhone you bought there. For any other number, connect Twilio. Nothing is "
    "placed or bought on a shared account."
)


async def require_own_twilio(db: AsyncSession, tenant_id: uuid.UUID) -> None:
    """Refuse when this call or message would be billed to the shared Twilio account."""
    if not await uses_platform_twilio(db, tenant_id):
        return
    settings = get_settings()
    if settings.twilio_account_sid and settings.twilio_auth_token:
        raise SharedAccountError(OWN_ACCOUNT_MESSAGE)


# --- which account a provider runs on: CallPlus's or the workspace's own ----------------------------------


async def _preferred(db: AsyncSession, tenant_id: uuid.UUID, provider: str) -> str | None:
    tenant = await db.get(Tenant, tenant_id)
    mode = ((tenant.provider_modes or {}) if tenant else {}).get(provider)
    return mode if mode in ("own", "managed") else None


async def resolve_key(db: AsyncSession, tenant_id: uuid.UUID, provider: str) -> tuple[str, str]:
    """(key, source) for a speech or Sarvam provider, source being "own", "managed" (the platform's account) or ""
    when neither is available. The workspace's choice is honoured; if it chose an account that is not there, the
    other one is used so a call never fails for a setting. With no choice, its own account comes first."""
    own = await _connected_integration(db, tenant_id, provider)
    prefer = await _preferred(db, tenant_id, provider)
    order = ("managed", "own") if prefer == "managed" else ("own", "managed")
    for source in order:
        if source == "own" and own is not None:
            return own[1], "own"
        if source == "managed" and managed.available(provider):
            return managed.platform_key(provider), "managed"
    return "", ""


async def key_for_source(db: AsyncSession, tenant_id: uuid.UUID, provider: str, source: str) -> str:
    """The key for one specific account ("own" or "managed"), whatever the workspace currently prefers. Used to reach
    a thing that was created on that account, such as a cloned voice, even after the preference changed."""
    if source == "managed":
        return managed.platform_key(provider)
    own = await _connected_integration(db, tenant_id, provider)
    return own[1] if own else ""


async def get_sarvam_api_key(db: AsyncSession, tenant_id: uuid.UUID) -> str:
    """The Sarvam key for this workspace: its own, or CallPlus's when that is offered and chosen. Empty when
    neither exists, so nothing is ever billed to a shared account that the platform did not switch on."""
    return (await resolve_key(db, tenant_id, SARVAM))[0]


async def get_provider_key(db: AsyncSession, tenant_id: uuid.UUID, provider: str) -> str:
    """The key to use for a speech provider (elevenlabs, deepgram): the workspace's own or CallPlus's."""
    return (await resolve_key(db, tenant_id, provider))[0]


async def resolve_llm(db: AsyncSession, tenant_id: uuid.UUID) -> tuple["str | LlmEndpoint", str]:
    """(what to pass as `api_key` when asking a model for a reply, source).

    Own OpenAI-compatible model if connected ("own"). CallPlus's separate model if offered and chosen
    ("managed"). Otherwise the Sarvam key from resolve_key: the workspace's own key wins unless it chose
    CallPlus. "managed" is a plan minute. "own" means the workspace pays that provider.
    """
    own = await _connected_integration(db, tenant_id, LLM)
    own_endpoint = None
    if own is not None:
        integration, key = own
        cfg = integration.config or {}
        if cfg.get("url") and cfg.get("model"):
            own_endpoint = LlmEndpoint(
                url=cfg["url"], model=cfg["model"], api_key=key, label=cfg.get("label") or "your language model"
            )
    prefer = await _preferred(db, tenant_id, LLM)
    order = ("managed", "own") if prefer == "managed" else ("own", "managed")
    for source in order:
        if source == "own" and own_endpoint is not None:
            return own_endpoint, "own"
        if source == "managed" and managed.available(LLM):
            endpoint = managed.platform_llm_endpoint()
            if endpoint is not None:
                return endpoint, "managed"
    # No separate model: Sarvam writes the reply, on the workspace's own key or CallPlus's.
    return await resolve_key(db, tenant_id, SARVAM)


async def get_llm_key(db: AsyncSession, tenant_id: uuid.UUID) -> "str | LlmEndpoint":
    """What to pass as `api_key` when asking a model for a reply (see resolve_llm)."""
    return (await resolve_llm(db, tenant_id))[0]


async def speech_settings(db: AsyncSession, tenant_id: uuid.UUID, agent) -> dict:
    """Which providers hear and speak for this agent, with the keys to use, for the realtime gateway, the voice to use,
    and what the call costs in minutes.

    An agent can choose Deepgram and ElevenLabs only while an account for them is available (the workspace's own, or
    CallPlus's); if it is gone (disconnected later) the agent falls back to Sarvam and a Sarvam voice, so a call never
    goes silent. A cloned voice is reached with the account it was made on, whatever the workspace prefers now.
    `cost_multiplier` is how many plan minutes one real minute costs. It is 1 when hearing, speaking or the
    reply runs on CallPlus, and 0 when every part uses the workspace's own keys. Deepgram or ElevenLabs on
    CallPlus add their extra minutes on top."""
    from app.services.voice_clones import clone_for_voice, is_sarvam_clone_id, voice_allowed

    stt = getattr(agent, "stt_provider", None) or SARVAM
    tts = getattr(agent, "tts_provider", None) or SARVAM
    voice = (getattr(agent, "voice", None) or "").strip()
    default_voice = "priya"
    sarvam_key, sarvam_source = await resolve_key(db, tenant_id, SARVAM)
    stt_key, stt_source = sarvam_key, sarvam_source
    tts_key, tts_source = sarvam_key, sarvam_source
    if stt == DEEPGRAM:
        stt_key, stt_source = await resolve_key(db, tenant_id, DEEPGRAM)
        if not stt_key:
            stt, stt_key, stt_source = SARVAM, sarvam_key, sarvam_source
    if tts == ELEVENLABS:
        tts_key, tts_source = await resolve_key(db, tenant_id, ELEVENLABS)
        clone = await clone_for_voice(db, tenant_id, ELEVENLABS, voice)
        if clone is not None:
            tts_source = clone.key_source
            tts_key = await key_for_source(db, tenant_id, ELEVENLABS, clone.key_source)
        elif tts_key and tts_source == "managed" and not await voice_allowed(db, tenant_id, voice):
            # On CallPlus's account a workspace may use premade voices and its own clones, never another's.
            tts_key = ""
        if not tts_key:
            tts, tts_key, tts_source, voice = SARVAM, sarvam_key, sarvam_source, default_voice
    elif is_sarvam_clone_id(voice):
        clone = await clone_for_voice(db, tenant_id, SARVAM, voice)
        clone_key = await key_for_source(db, tenant_id, SARVAM, clone.key_source) if clone is not None else ""
        if clone_key:
            tts_key, tts_source = clone_key, clone.key_source
        else:
            voice = default_voice  # not this workspace's voice, or its account is gone
    model = ""
    if tts == ELEVENLABS:
        if tts_source == "own":
            connected = await _connected_integration(db, tenant_id, ELEVENLABS)
            model = ((connected[0].config or {}).get("model") or "") if connected else ""
        else:
            model = get_settings().elevenlabs_model
    llm_key, llm_source = await resolve_llm(db, tenant_id)
    # Included price when CallPlus pays a provider. Own keys only: charge 0.
    # Free only when every part really runs on the workspace's own key. A part with no key at all is not
    # "own", so it stays billable: never let a missing key turn into free minutes.
    all_own = all(source == "own" for source in (stt_source, tts_source, llm_source))
    multiplier = 0.0 if all_own else 1.0
    for provider, source in ((stt, stt_source), (tts, tts_source)):
        if source == "managed" and provider != SARVAM:
            multiplier += managed.extra_minutes(provider)
    if isinstance(llm_key, LlmEndpoint) and llm_source == "managed":
        multiplier += managed.extra_minutes(LLM)
    return {
        "stt_provider": stt,
        "stt_api_key": stt_key,
        "tts_provider": tts,
        "tts_api_key": tts_key,
        "tts_model": model,
        "voice": voice or (default_voice if tts == SARVAM else ""),
        "cost_multiplier": round(multiplier, 2),
    }
