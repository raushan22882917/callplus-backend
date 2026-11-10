"""Cloning a voice: the consent a person gives, and who may use which voice.

A clone is made from a short recording the speaker reads aloud after agreeing to a statement. The statement and its
version are stored with the voice. Voices on CallPlus's own provider account are shared by every workspace there, so a
workspace may only use premade voices and its own clones on it; otherwise one business could speak in another's voice.
"""

import logging
import time
import uuid

from app.integrations import managed
from app.integrations.credentials import resolve_key
from app.integrations.providers import ELEVENLABS
from app.integrations.speech_providers import SpeechProviderError, elevenlabs_voices
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import VoiceClone

logger = logging.getLogger(__name__)

CONSENT_VERSION = "2026-11-v1"

PROVIDER_NAMES = {"elevenlabs": "ElevenLabs", "sarvam": "Sarvam AI"}


def consent_text(provider: str = "elevenlabs") -> str:
    """What the speaker agrees to. It names the company that receives the recording, so it differs by provider."""
    return (
        "I confirm that this is my own voice, or that the person whose voice it is has given me their written permission "
        f"to do this. I agree that this recording is sent to {PROVIDER_NAMES.get(provider, provider)} to make a synthetic "
        "copy of the voice, and that my business's AI agents may speak to callers with it. I understand callers will be "
        "talking to an AI assistant, and that the voice can be deleted at any time."
    )


CONSENT_TEXT = consent_text("elevenlabs")  # kept for callers that want the default wording

# About half a minute when read aloud at a normal pace, with a range of sounds, which makes a better clone than
# free-form speech. The speaker reads it right after agreeing to the statement above.
READING_PASSAGE = (
    "Hello, and thank you for calling. I can help you book an appointment, check an order, or answer questions about "
    "our opening hours and prices. Please tell me what you need, and I will do my best to sort it out quickly. If it "
    "is easier, I can also send the details to you by message. Our team is here every day from nine in the morning "
    "until six in the evening, and on Saturdays until two. Would you like me to find a time that suits you this week? "
    "Wonderful, I have noted that down. Is there anything else I can help you with today?"
)

MIN_SECONDS = 20  # shorter samples make a poor clone
MAX_SECONDS = 600
MAX_BYTES = 25 * 1024 * 1024
MIN_BYTES = 20 * 1024
AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".ogg", ".flac", ".webm", ".aac", ".opus"}

_premade_cache: tuple[float, set[str]] | None = None
_PREMADE_TTL = 600.0


async def _premade_ids(api_key: str) -> set[str]:
    """Ids of ElevenLabs' own library voices, which any workspace may use. Cached for a few minutes."""
    global _premade_cache
    now = time.monotonic()
    if _premade_cache and now - _premade_cache[0] < _PREMADE_TTL:
        return _premade_cache[1]
    voices = await elevenlabs_voices(api_key)
    ids = {v["voice_id"] for v in voices if (v.get("category") or "") == "premade"}
    _premade_cache = (now, ids)
    return ids


async def voice_allowed(db: AsyncSession, tenant_id: uuid.UUID, voice_id: str) -> bool:
    """May this workspace speak with this ElevenLabs voice id?

    On its own ElevenLabs account, any voice it can name is its own. On CallPlus's account only premade voices and
    the workspace's own clones are allowed. If CallPlus's library cannot be read, the answer is no."""
    _key, source = await resolve_key(db, tenant_id, ELEVENLABS)
    if source != "managed":
        return True
    mine = await db.execute(
        select(VoiceClone.id).where(VoiceClone.tenant_id == tenant_id, VoiceClone.provider_voice_id == voice_id)
    )
    if mine.first() is not None:
        return True
    try:
        return voice_id in await _premade_ids(managed.platform_key(ELEVENLABS))
    except SpeechProviderError:
        logger.warning("could not read CallPlus's ElevenLabs library to check voice %s", voice_id)
        return False


async def clone_for_voice(db: AsyncSession, tenant_id: uuid.UUID, provider: str, voice_id: str | None) -> VoiceClone | None:
    """The workspace's own clone that has this voice id, if any."""
    if not voice_id:
        return None
    row = await db.execute(
        select(VoiceClone).where(
            VoiceClone.tenant_id == tenant_id, VoiceClone.provider == provider, VoiceClone.provider_voice_id == voice_id
        )
    )
    return row.scalar_one_or_none()


def is_sarvam_clone_id(voice: str | None) -> bool:
    from app.integrations.speech_providers import SARVAM_CLONE_PREFIX

    return bool(voice) and voice.startswith(SARVAM_CLONE_PREFIX)
