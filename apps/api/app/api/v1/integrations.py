"""Tenant self-service connection of their own (BYO) Twilio account or
Sarvam API key. Connecting one here means every call/message/turn for this
tenant uses their credentials instead of the platform's shared ones — see
app/integrations/credentials.py for where that's resolved."""

import asyncio
import uuid

from app.core.crypto import encrypt_secret
from app.core.audit import log_audit_event
from app.core.deps import RequestContext, get_db, require_permission
from app.core.permissions import Permissions
from app.integrations.credentials import (
    OWN_ACCOUNT_MESSAGE,
    TwilioCredentials,
    get_provider_key,
    get_twilio_credentials,
    resolve_key,
)
from app.integrations import managed, providers
from app.integrations.llm import LlmError, check_endpoint, endpoint_from
from app.integrations.sarvam import SarvamAPIError, check_api_key
from app.integrations.speech_providers import (
    SpeechProviderError,
    check_deepgram_key,
    check_elevenlabs_key,
    elevenlabs_voices,
)
from app.integrations.twilio_client import check_account
from app.schemas.integration import (
    ConnectEmailSenderRequest,
    ConnectDeepgramRequest,
    ConnectElevenLabsRequest,
    ConnectExotelRequest,
    ConnectLlmRequest,
    ConnectSarvamRequest,
    SetProviderModeRequest,
    ConnectTwilioRequest,
    ExotelOutboundRequest,
    ExotelSetup,
    IntegrationRead,
)
from app.services import connection_health
from app.services import exotel as exotel_service
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import (
    Integration,
    IntegrationCredential,
    IntegrationStatus,
    Tenant,
    VoiceClone,
    WhatsAppAccount,
)

router = APIRouter(prefix="/integrations", tags=["integrations"])


async def _upsert_integration(
    db: AsyncSession, *, tenant_id: uuid.UUID, provider: str, config: dict, secret: str
) -> Integration:
    result = await db.execute(
        select(Integration).where(
            Integration.tenant_id == tenant_id, Integration.provider == provider
        )
    )
    integration = result.scalar_one_or_none()

    if integration is None:
        integration = Integration(
            tenant_id=tenant_id,
            provider=provider,
            status=IntegrationStatus.CONNECTED,
            config=config,
        )
        db.add(integration)
        await db.flush()
        db.add(
            IntegrationCredential(
                tenant_id=tenant_id,
                integration_id=integration.id,
                encrypted_secret=encrypt_secret(secret),
            )
        )
        return integration

    integration.config = config
    integration.status = IntegrationStatus.CONNECTED
    cred_result = await db.execute(
        select(IntegrationCredential).where(IntegrationCredential.integration_id == integration.id)
    )
    credential = cred_result.scalar_one_or_none()
    if credential is None:
        db.add(
            IntegrationCredential(
                tenant_id=tenant_id,
                integration_id=integration.id,
                encrypted_secret=encrypt_secret(secret),
            )
        )
    else:
        credential.encrypted_secret = encrypt_secret(secret)
    connection_health.forget(tenant_id)
    return integration


@router.get("", response_model=list[IntegrationRead])
async def list_integrations(
    ctx: RequestContext = Depends(require_permission(Permissions.INTEGRATIONS_READ)),
    db: AsyncSession = Depends(get_db),
) -> list[Integration]:
    result = await db.execute(select(Integration).where(Integration.tenant_id == ctx.tenant_id))
    return list(result.scalars().all())


async def _owns(db: AsyncSession, tenant_id: uuid.UUID, provider: str) -> bool:
    row = await db.execute(
        select(Integration.id).where(
            Integration.tenant_id == tenant_id,
            Integration.provider == provider,
            Integration.status == IntegrationStatus.CONNECTED,
        )
    )
    return row.first() is not None


@router.get("/included")
async def included_services(
    ctx: RequestContext = Depends(require_permission(Permissions.INTEGRATIONS_READ)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, bool]:
    """Which of its own accounts this workspace has connected. Nothing here is covered by a
    shared platform key."""
    whatsapp = await db.execute(
        select(WhatsAppAccount.id).where(WhatsAppAccount.tenant_id == ctx.tenant_id).limit(1)
    )
    return {
        "exotel": await _owns(db, ctx.tenant_id, "exotel"),
        "twilio": await _owns(db, ctx.tenant_id, "twilio"),
        "sarvam": await _owns(db, ctx.tenant_id, "sarvam"),
        "whatsapp": whatsapp.first() is not None,
    }


@router.get("/setup")
async def setup_status(
    ctx: RequestContext = Depends(require_permission(Permissions.INTEGRATIONS_READ)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """What this workspace still has to connect before it can call and answer on its own accounts."""
    connected = await included_services(ctx, db)
    items = [
        {
            "id": "exotel",
            "connected": connected["exotel"],
            "title": "Indian phone number",
            "detail": (
                "Buy an ExoPhone in your own Exotel account, then connect Exotel here and add "
                "that number under Phone numbers."
            ),
        },
        {
            "id": "twilio",
            "connected": connected["twilio"],
            "title": "Twilio",
            "detail": "Connect your own Twilio account if you call from a Twilio number or send SMS.",
        },
        {
            "id": "sarvam",
            "connected": connected["sarvam"],
            "title": "Sarvam voice",
            "detail": "Paste your own Sarvam API key. The assistant speaks and listens on your account.",
        },
        {
            "id": "whatsapp",
            "connected": connected["whatsapp"],
            "title": "WhatsApp",
            "detail": "Connect your own WhatsApp Business number.",
        },
    ]
    calls_ready = connected["exotel"] or connected["twilio"]
    # CallPlus's Sarvam key is enough. An own key is the other price, not a requirement.
    sarvam_ready = connected["sarvam"] or managed.available("sarvam")
    if managed.available("sarvam") and not connected["sarvam"]:
        items[2]["connected"] = True
        items[2]["detail"] = (
            "Included. CallPlus's key is used and each minute comes out of your plan or wallet. "
            "Add your own key only if you want Sarvam to bill you instead."
        )
    elif connected["sarvam"]:
        items[2]["detail"] = (
            "Your key is connected. On it you pay Sarvam and CallPlus does not charge the minutes. "
            "On CallPlus, the minutes come out of your plan or wallet."
        )
    return {
        "ready_for_calls": calls_ready and sarvam_ready,
        "message": None
        if calls_ready and sarvam_ready
        else OWN_ACCOUNT_MESSAGE,
        "items": items,
    }


@router.post("/twilio", response_model=IntegrationRead, status_code=status.HTTP_201_CREATED)
async def connect_twilio(
    payload: ConnectTwilioRequest,
    ctx: RequestContext = Depends(require_permission(Permissions.INTEGRATIONS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> Integration:
    # Check with Twilio first: saving details Twilio will refuse only produces a false "Connected".
    ok, message = await asyncio.to_thread(
        check_account,
        TwilioCredentials(account_sid=payload.account_sid, auth_token=payload.auth_token),
    )
    if not ok:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, message)
    integration = await _upsert_integration(
        db,
        tenant_id=ctx.tenant_id,
        provider="twilio",
        config={"account_sid": payload.account_sid},
        secret=payload.auth_token,
    )
    await db.commit()
    await db.refresh(integration)
    return integration


@router.post("/email-sender", response_model=IntegrationRead, status_code=status.HTTP_201_CREATED)
async def connect_email_sender(
    payload: ConnectEmailSenderRequest,
    ctx: RequestContext = Depends(require_permission(Permissions.INTEGRATIONS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> Integration:
    """Saves this workspace's own Resend or Brevo key, after the provider confirms the key and the sender address.
    The key is encrypted and is used for this workspace's customer email."""
    from app.integrations import email_providers

    try:
        await email_providers.check_sender(payload.provider, payload.api_key, str(payload.from_email))
    except email_providers.EmailProviderError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    integration = await _upsert_integration(
        db,
        tenant_id=ctx.tenant_id,
        provider="email_sender",
        config={
            "provider": payload.provider,
            "from_email": str(payload.from_email).lower(),
            **({"from_name": payload.from_name.strip()} if payload.from_name and payload.from_name.strip() else {}),
        },
        secret=payload.api_key.strip(),
    )
    await log_audit_event(
        db, tenant_id=ctx.tenant_id, actor_user_id=ctx.user_id, action="integration.email_sender_connected",
        resource_type="integration", resource_id=integration.id, extra={"provider": payload.provider},
    )
    await db.commit()
    await db.refresh(integration)
    return integration


@router.get("/health")
async def connections_health(
    force: bool = False,
    ctx: RequestContext = Depends(require_permission(Permissions.INTEGRATIONS_READ)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Asks each connected provider whether it still accepts this workspace's credentials. Kept for five minutes;
    `force` asks again, at most once every fifteen seconds."""
    return await connection_health.check_all(db, ctx.tenant_id, force=force)


@router.get("/twilio/status")
async def twilio_status(
    ctx: RequestContext = Depends(require_permission(Permissions.INTEGRATIONS_READ)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    """Would Twilio accept a call from this workspace right now? Uses the workspace's own account
    if it saved one, otherwise the platform's."""
    ok, message = await asyncio.to_thread(check_account, await get_twilio_credentials(db, ctx.tenant_id))
    return {"ok": ok, "message": message}


@router.post("/sarvam", response_model=IntegrationRead, status_code=status.HTTP_201_CREATED)
async def connect_sarvam(
    payload: ConnectSarvamRequest,
    ctx: RequestContext = Depends(require_permission(Permissions.INTEGRATIONS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> Integration:
    try:
        await check_api_key(payload.api_key)
    except SarvamAPIError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    integration = await _upsert_integration(
        db, tenant_id=ctx.tenant_id, provider="sarvam", config={}, secret=payload.api_key
    )
    await db.commit()
    await db.refresh(integration)
    return integration


ELEVENLABS_DEFAULT_MODEL = "eleven_flash_v2_5"


@router.post("/elevenlabs", response_model=IntegrationRead, status_code=status.HTTP_201_CREATED)
async def connect_elevenlabs(
    payload: ConnectElevenLabsRequest,
    ctx: RequestContext = Depends(require_permission(Permissions.INTEGRATIONS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> Integration:
    """Bring your own ElevenLabs account so agents can speak in its voices, in dozens of languages."""
    try:
        await check_elevenlabs_key(payload.api_key)
    except SpeechProviderError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    integration = await _upsert_integration(
        db,
        tenant_id=ctx.tenant_id,
        provider=providers.ELEVENLABS,
        config={"model": (payload.model or ELEVENLABS_DEFAULT_MODEL).strip()},
        secret=payload.api_key.strip(),
    )
    await db.commit()
    await db.refresh(integration)
    return integration


@router.get("/elevenlabs/voices")
async def list_elevenlabs_voices(
    ctx: RequestContext = Depends(require_permission(Permissions.INTEGRATIONS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """The voices an agent can speak with: the library, plus this workspace's own cloned voices. On CallPlus's shared
    account only premade voices are listed, because the account also holds other workspaces' clones."""
    key, source = await resolve_key(db, ctx.tenant_id, providers.ELEVENLABS)
    if not key:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Connect your ElevenLabs account first.")
    clones = (
        await db.execute(select(VoiceClone).where(VoiceClone.tenant_id == ctx.tenant_id).order_by(VoiceClone.created_at.desc()))
    ).scalars().all()
    mine = [
        {
            "voice_id": c.provider_voice_id,
            "name": f"{c.name} (cloned)",
            "category": "cloned",
            "labels": {"language": c.language} if c.language else {},
            "preview_url": None,
            "clone_id": str(c.id),
        }
        for c in clones
    ]
    try:
        voices = await elevenlabs_voices(key)
    except SpeechProviderError as exc:
        if mine:  # a key that cannot list voices can still speak with the ones we made
            return mine
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    if source == "managed":
        voices = [v for v in voices if (v.get("category") or "") == "premade"]
    own_ids = {m["voice_id"] for m in mine}
    return mine + [v for v in voices if v["voice_id"] not in own_ids]


@router.post("/deepgram", response_model=IntegrationRead, status_code=status.HTTP_201_CREATED)
async def connect_deepgram(
    payload: ConnectDeepgramRequest,
    ctx: RequestContext = Depends(require_permission(Permissions.INTEGRATIONS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> Integration:
    """Bring your own Deepgram account so agents can hear callers in dozens of languages."""
    try:
        await check_deepgram_key(payload.api_key)
    except SpeechProviderError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    integration = await _upsert_integration(
        db, tenant_id=ctx.tenant_id, provider=providers.DEEPGRAM, config={}, secret=payload.api_key.strip()
    )
    await db.commit()
    await db.refresh(integration)
    return integration


@router.post("/llm", response_model=IntegrationRead, status_code=status.HTTP_201_CREATED)
async def connect_llm(
    payload: ConnectLlmRequest,
    ctx: RequestContext = Depends(require_permission(Permissions.INTEGRATIONS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> Integration:
    """Bring your own language model (OpenAI, Gemini, Groq, Mistral or any OpenAI-compatible service). Every agent in
    the workspace then writes its replies with it instead of Sarvam's model."""
    try:
        endpoint = endpoint_from(payload.preset, api_key=payload.api_key, model=payload.model, base_url=payload.base_url)
        await check_endpoint(endpoint)
    except LlmError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    integration = await _upsert_integration(
        db,
        tenant_id=ctx.tenant_id,
        provider=providers.LLM,
        config={"preset": payload.preset, "label": endpoint.label, "url": endpoint.url, "model": endpoint.model},
        secret=endpoint.api_key,
    )
    await db.commit()
    await db.refresh(integration)
    return integration


MODE_LABELS = {
    providers.SARVAM: "Sarvam (India voice and replies)",
    providers.DEEPGRAM: "Deepgram (hearing, worldwide)",
    providers.ELEVENLABS: "ElevenLabs (voices, worldwide)",
    providers.LLM: "Language model",
}


async def _provider_state(db: AsyncSession, tenant: Tenant) -> list[dict]:
    """For each provider: whether the workspace has its own account, whether CallPlus's is offered, what it chose and
    what a call will actually use."""
    rows = (await db.execute(select(Integration).where(Integration.tenant_id == tenant.id))).scalars().all()
    own = {r.provider for r in rows if r.status == IntegrationStatus.CONNECTED}
    saved = tenant.provider_modes or {}
    out = []
    for provider in managed.PROVIDERS:
        offered = managed.available(provider)
        has_own = provider in own
        choice = saved.get(provider) if saved.get(provider) in ("own", "managed") else None
        order = ("managed", "own") if choice == "managed" else ("own", "managed")
        using = "none"
        for source in order:
            if (source == "own" and has_own) or (source == "managed" and offered):
                using = source
                break
        out.append(
            {
                "provider": provider,
                "label": MODE_LABELS[provider],
                "own_connected": has_own,
                "managed_available": offered,
                "choice": choice,
                "using": using,
                # Extra minutes counted per call minute when this runs on CallPlus's account.
                "managed_extra_minutes": managed.extra_minutes(provider),
            }
        )
    return out


@router.get("/modes")
async def provider_modes(
    ctx: RequestContext = Depends(require_permission(Permissions.INTEGRATIONS_READ)),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """Per provider: use CallPlus's account (nothing to set up) or the workspace's own."""
    tenant = await db.get(Tenant, ctx.tenant_id)
    return await _provider_state(db, tenant)


@router.put("/modes")
async def set_provider_mode(
    payload: SetProviderModeRequest,
    ctx: RequestContext = Depends(require_permission(Permissions.INTEGRATIONS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    if payload.provider not in managed.PROVIDERS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Unknown provider.")
    tenant = await db.get(Tenant, ctx.tenant_id)
    state = {s["provider"]: s for s in await _provider_state(db, tenant)}[payload.provider]
    if payload.mode == "managed" and not state["managed_available"]:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "CallPlus's account is not offered for this provider.")
    if payload.mode == "own" and not state["own_connected"]:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "Connect your own account for this provider first."
        )
    modes = dict(tenant.provider_modes or {})
    if payload.mode is None:
        modes.pop(payload.provider, None)
    else:
        modes[payload.provider] = payload.mode
    tenant.provider_modes = modes
    await db.commit()
    await db.refresh(tenant)
    return await _provider_state(db, tenant)


@router.get("/providers")
async def provider_catalogue(
    ctx: RequestContext = Depends(require_permission(Permissions.INTEGRATIONS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """What the agent form and the integrations page offer: providers, languages and which are usable, either on the
    workspace's own account or on CallPlus's."""
    tenant = await db.get(Tenant, ctx.tenant_id)
    state = {s["provider"]: s for s in await _provider_state(db, tenant)}
    rows = (await db.execute(select(Integration).where(Integration.tenant_id == ctx.tenant_id))).scalars().all()
    llm = next((r.config for r in rows if r.provider == providers.LLM and r.status == IntegrationStatus.CONNECTED), None)

    def usable(provider: str) -> bool:
        s = state.get(provider)
        return provider == providers.SARVAM or bool(s and (s["own_connected"] or s["managed_available"]))

    return {
        "stt": [{"id": k, "label": v, "connected": usable(k)} for k, v in providers.STT_PROVIDERS.items()],
        "tts": [{"id": k, "label": v, "connected": usable(k)} for k, v in providers.TTS_PROVIDERS.items()],
        "languages": {
            "indian": providers.SARVAM_LANGUAGES,
            "international": providers.INTERNATIONAL_LANGUAGES,
        },
        "llm_presets": {k: {"label": v["label"], "model": v["model"], "url": v["url"]} for k, v in providers.LLM_PRESETS.items()},
        "llm": {"connected": llm is not None, "label": (llm or {}).get("label"), "model": (llm or {}).get("model")},
        "modes": list(state.values()),
    }


def _exotel_steps() -> list[str]:
    return [
        "In Exotel, open App Bazaar and create a new flow (or edit the one your number uses).",
        "Add a Voicebot applet and paste the stream address below into its URL box. "
        "Keep the sample rate at 8000.",
        "Add a Hangup applet after the Voicebot applet.",
        "Attach your Exotel number (ExoPhone) to this flow.",
        "In CallPlus, add the same number under Phone numbers, choose Exotel, "
        "and pick the AI agent.",
        "To place calls from this number too, copy the flow's App ID (next to it in App Bazaar) "
        "into Integrations → Exotel → Outgoing calls.",
    ]


@router.post("/exotel", response_model=ExotelSetup, status_code=status.HTTP_201_CREATED)
async def connect_exotel(
    payload: ConnectExotelRequest,
    ctx: RequestContext = Depends(require_permission(Permissions.INTEGRATIONS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> ExotelSetup:
    """Saves this workspace's own Exotel account after checking the details with Exotel, and
    returns the private stream address to paste into its Exotel flow (shown only now)."""
    try:
        await exotel_service.check_credentials(
            subdomain=payload.subdomain,
            account_sid=payload.account_sid.strip(),
            api_key=payload.api_key.strip(),
            api_token=payload.api_token.strip(),
        )
    except exotel_service.ExotelError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    integration, key = await exotel_service.connect(
        db,
        ctx.tenant_id,
        subdomain=payload.subdomain,
        account_sid=payload.account_sid.strip(),
        api_key=payload.api_key.strip(),
        api_token=payload.api_token.strip(),
    )
    await db.commit()
    await db.refresh(integration)
    connection_health.forget(ctx.tenant_id)
    return ExotelSetup(
        integration=integration, stream_url=exotel_service.stream_url(key), steps=_exotel_steps()
    )


@router.post("/exotel/stream-address", response_model=ExotelSetup)
async def new_exotel_stream_address(
    ctx: RequestContext = Depends(require_permission(Permissions.INTEGRATIONS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> ExotelSetup:
    """A fresh stream address; the old one stops working. For a leaked or lost address."""
    try:
        key = await exotel_service.regenerate_stream_key(db, ctx.tenant_id)
    except exotel_service.ExotelError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    await db.commit()
    result = await db.execute(
        select(Integration).where(
            Integration.tenant_id == ctx.tenant_id, Integration.provider == "exotel"
        )
    )
    return ExotelSetup(
        integration=result.scalar_one(),
        stream_url=exotel_service.stream_url(key),
        steps=_exotel_steps(),
    )


@router.get("/exotel/numbers")
async def exotel_numbers(
    ctx: RequestContext = Depends(require_permission(Permissions.INTEGRATIONS_READ)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """ExoPhones already on the workspace's Exotel account. Buying one still happens in Exotel, after KYC."""
    try:
        numbers = await exotel_service.list_incoming_numbers(db, ctx.tenant_id)
    except exotel_service.ExotelError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return {"numbers": numbers}


@router.put("/exotel/outbound", response_model=IntegrationRead)
async def set_exotel_outbound_flow(
    payload: ExotelOutboundRequest,
    ctx: RequestContext = Depends(require_permission(Permissions.INTEGRATIONS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> Integration:
    """Saves which Exotel flow an answered outbound call runs, so the dialer can call from
    Exotel numbers."""
    try:
        integration = await exotel_service.set_outbound_app_id(db, ctx.tenant_id, payload.app_id)
    except exotel_service.ExotelError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    await db.commit()
    await db.refresh(integration)
    return integration


@router.delete("/{provider}", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect_integration(
    provider: str,
    ctx: RequestContext = Depends(require_permission(Permissions.INTEGRATIONS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> None:
    result = await db.execute(
        select(Integration).where(
            Integration.tenant_id == ctx.tenant_id, Integration.provider == provider
        )
    )
    integration = result.scalar_one_or_none()
    if integration is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Integration not connected")
    await db.delete(integration)
    await db.commit()
    connection_health.forget(ctx.tenant_id)
