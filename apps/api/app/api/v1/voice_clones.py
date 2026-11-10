"""Cloning a voice for the workspace's AI agents.

A person records themselves reading a short passage (or uploads a recording), after agreeing to a consent statement.
The recording goes to ElevenLabs, which returns a voice id; the voice then appears in the agent's voice list. See
app/services/voice_clones.py for the consent text and the rule about whose voices a workspace may use.
"""

import os
import uuid
from datetime import UTC, datetime

from app.core.audit import log_audit_event
from app.core.config import get_settings
from app.core.deps import RequestContext, get_db, require_permission
from app.core.permissions import Permissions
from app.integrations import managed
from app.integrations.credentials import key_for_source, resolve_key
from app.integrations.providers import ELEVENLABS, SARVAM
from app.integrations.speech_providers import (
    SARVAM_CLONE_LANGUAGES,
    SpeechProviderError,
    create_instant_clone,
    create_sarvam_clone,
    delete_elevenlabs_voice,
    delete_sarvam_voice,
    elevenlabs_preview,
    sarvam_clone_preview,
)
from app.services import channels as rate
from app.services import voice_clones as vc
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import AIAgent, VoiceClone

router = APIRouter(prefix="/voice-clones", tags=["voice-clones"])

PROVIDERS = (ELEVENLABS, SARVAM)
PREVIEW_DEFAULT = "Hello, this is how I will sound when I answer your calls. How can I help you today?"


class VoiceCloneRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    provider: str
    provider_voice_id: str
    name: str
    speaker_name: str
    language: str | None
    key_source: str
    status: str
    requires_verification: bool
    sample_seconds: int | None
    consent_version: str
    consent_at: datetime
    created_at: datetime


class PreviewRequest(BaseModel):
    text: str = Field(default=PREVIEW_DEFAULT, min_length=1, max_length=240)


@router.get("/script")
async def consent_and_script(
    provider: str = Query(default=ELEVENLABS),
    ctx: RequestContext = Depends(require_permission(Permissions.AGENTS_READ)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """What the speaker agrees to, and the passage they read aloud, plus how many voices the workspace may keep."""
    if provider not in PROVIDERS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Unknown voice provider.")
    _key, source = await resolve_key(db, ctx.tenant_id, provider)
    settings = get_settings()
    used = (await db.execute(select(func.count()).select_from(VoiceClone).where(VoiceClone.tenant_id == ctx.tenant_id))).scalar_one()
    return {
        "provider": provider,
        "version": vc.CONSENT_VERSION,
        "consent_text": vc.consent_text(provider),
        "reading_passage": vc.READING_PASSAGE,
        "min_seconds": vc.MIN_SECONDS,
        "max_seconds": vc.MAX_SECONDS,
        "available": bool(source),
        "source": source or None,
        "limit": settings.voice_clones_managed_cap if source == "managed" else settings.voice_clones_own_cap,
        "used": used,
        # Sarvam needs the language the voice will speak; ElevenLabs works it out.
        "languages": SARVAM_CLONE_LANGUAGES if provider == SARVAM else None,
    }


@router.get("", response_model=list[VoiceCloneRead])
async def list_clones(
    ctx: RequestContext = Depends(require_permission(Permissions.AGENTS_READ)),
    db: AsyncSession = Depends(get_db),
) -> list[VoiceClone]:
    rows = await db.execute(
        select(VoiceClone).where(VoiceClone.tenant_id == ctx.tenant_id).order_by(VoiceClone.created_at.desc())
    )
    return list(rows.scalars().all())


@router.post("", response_model=VoiceCloneRead, status_code=status.HTTP_201_CREATED)
async def create_clone(
    name: str = Form(..., min_length=1, max_length=80),
    speaker_name: str = Form(..., min_length=1, max_length=120),
    consent: bool = Form(...),
    consent_version: str = Form(...),
    duration_seconds: float = Form(...),
    language: str | None = Form(default=None, max_length=10),
    provider: str = Form(default=ELEVENLABS),
    remove_background_noise: bool = Form(default=False),
    file: UploadFile = File(...),
    ctx: RequestContext = Depends(require_permission(Permissions.AGENTS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> VoiceClone:
    if provider not in PROVIDERS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Unknown voice provider.")
    if provider == SARVAM and language not in SARVAM_CLONE_LANGUAGES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Choose the language this voice will speak (Sarvam voices speak Indian languages and English).",
        )
    if not consent or consent_version != vc.CONSENT_VERSION:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "The speaker must agree to the consent statement before a voice is cloned.",
        )
    if not (vc.MIN_SECONDS <= duration_seconds <= vc.MAX_SECONDS):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"The recording must be between {vc.MIN_SECONDS} seconds and {vc.MAX_SECONDS // 60} minutes long.",
        )
    audio = await file.read()
    extension = os.path.splitext(file.filename or "")[1].lower()
    if not ((file.content_type or "").startswith("audio/") or extension in vc.AUDIO_EXTENSIONS):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Upload an audio recording (WAV, MP3, M4A, OGG or FLAC).")
    if len(audio) < vc.MIN_BYTES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "That recording is too short or empty.")
    if len(audio) > vc.MAX_BYTES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "That recording is too large (25 MB at most).")

    key, source = await resolve_key(db, ctx.tenant_id, provider)
    if not key:
        label = vc.PROVIDER_NAMES[provider]
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Connect your {label} account under Integrations (or use CallPlus's) before cloning a voice.",
        )
    settings = get_settings()
    cap = settings.voice_clones_managed_cap if source == "managed" else settings.voice_clones_own_cap
    used = (
        await db.execute(
            select(func.count()).select_from(VoiceClone).where(VoiceClone.tenant_id == ctx.tenant_id, VoiceClone.key_source == source)
        )
    ).scalar_one()
    if used >= cap:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"This workspace can keep {cap} cloned voice{'s' if cap != 1 else ''} on this account. Delete one you no longer use first.",
        )
    if await rate.hit_rate_limit(f"voiceclone:{ctx.tenant_id}", limit=5, window_seconds=3600):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many voices created in the last hour. Try again later.")

    shown = f"{name.strip()} ({str(ctx.tenant_id)[:8]})" if source == "managed" else name.strip()
    try:
        if provider == SARVAM:
            voice_id = await create_sarvam_clone(
                key,
                name=shown,
                audio=audio,
                filename=file.filename or "voice.wav",
                content_type=file.content_type or "audio/wav",
                language=language or "",
            )
            needs_verification = False
        else:
            voice_id, needs_verification = await create_instant_clone(
                key,
                name=shown,
                audio=audio,
                filename=file.filename or "voice.wav",
                content_type=file.content_type or "audio/wav",
                description=f"Voice of {speaker_name.strip()}, cloned with their consent in CallPlus.",
                labels={"language": language} if language else None,
                remove_background_noise=remove_background_noise,
            )
    except SpeechProviderError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    clone = VoiceClone(
        tenant_id=ctx.tenant_id,
        provider=provider,
        provider_voice_id=voice_id,
        name=name.strip(),
        speaker_name=speaker_name.strip(),
        language=language,
        key_source=source,
        status="needs_verification" if needs_verification else "ready",
        requires_verification=needs_verification,
        sample_seconds=int(duration_seconds),
        consent_version=vc.CONSENT_VERSION,
        consent_text=vc.consent_text(provider),
        consent_at=datetime.now(UTC),
        created_by_user_id=ctx.user_id,
    )
    db.add(clone)
    await db.flush()
    await log_audit_event(
        db,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        action="voice_clone.created",
        resource_type="voice_clone",
        resource_id=clone.id,
        extra={"speaker_name": clone.speaker_name, "consent_version": vc.CONSENT_VERSION, "source": source, "provider": provider},
    )
    await db.commit()
    await db.refresh(clone)
    return clone


async def _mine(db: AsyncSession, tenant_id: uuid.UUID, clone_id: uuid.UUID) -> VoiceClone:
    clone = await db.get(VoiceClone, clone_id)
    if clone is None or clone.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Voice not found")
    return clone


@router.post("/{clone_id}/preview")
async def preview_clone(
    clone_id: uuid.UUID,
    payload: PreviewRequest,
    ctx: RequestContext = Depends(require_permission(Permissions.AGENTS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> Response:
    clone = await _mine(db, ctx.tenant_id, clone_id)
    if await rate.hit_rate_limit(f"voiceclone-preview:{ctx.tenant_id}", limit=20, window_seconds=60):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many previews; wait a moment.")
    key = await key_for_source(db, ctx.tenant_id, clone.provider, clone.key_source)
    if not key:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"The {vc.PROVIDER_NAMES[clone.provider]} account for this voice is not connected."
        )
    try:
        if clone.provider == SARVAM:
            audio = await sarvam_clone_preview(key, clone.provider_voice_id, payload.text.strip(), clone.language)
        else:
            model = await _own_model(db, ctx.tenant_id) if clone.key_source == "own" else get_settings().elevenlabs_model
            audio = await elevenlabs_preview(key, clone.provider_voice_id, payload.text.strip(), model)
    except SpeechProviderError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return Response(content=audio, media_type="audio/mpeg", headers={"Cache-Control": "no-store"})


async def _own_model(db: AsyncSession, tenant_id: uuid.UUID) -> str:
    from app.integrations.credentials import _connected_integration

    connected = await _connected_integration(db, tenant_id, ELEVENLABS)
    return ((connected[0].config or {}).get("model") or "") if connected else ""


@router.delete("/{clone_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_clone(
    clone_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(Permissions.AGENTS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> None:
    clone = await _mine(db, ctx.tenant_id, clone_id)
    using = (
        await db.execute(
            select(AIAgent.name).where(
                AIAgent.tenant_id == ctx.tenant_id,
                AIAgent.tts_provider == clone.provider,
                AIAgent.voice == clone.provider_voice_id,
            )
        )
    ).scalars().all()
    if using:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"This voice is used by {', '.join(using)}. Choose another voice for it first.",
        )
    key = await key_for_source(db, ctx.tenant_id, clone.provider, clone.key_source)
    if key:
        try:
            if clone.provider == SARVAM:
                await delete_sarvam_voice(key, clone.provider_voice_id)
            else:
                await delete_elevenlabs_voice(key, clone.provider_voice_id)
        except SpeechProviderError as exc:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                f"The voice could not be removed from {vc.PROVIDER_NAMES[clone.provider]} yet ({exc}). Try again.",
            ) from exc
    await log_audit_event(
        db,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        action="voice_clone.deleted",
        resource_type="voice_clone",
        resource_id=clone.id,
        extra={"speaker_name": clone.speaker_name},
    )
    await db.delete(clone)
    await db.commit()
