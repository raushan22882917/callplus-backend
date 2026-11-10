"""Checks and lookups for the speech providers a workspace can connect: ElevenLabs (voices) and Deepgram (hearing)."""

import httpx

ELEVENLABS_API = "https://api.elevenlabs.io"
DEEPGRAM_API = "https://api.deepgram.com"


class SpeechProviderError(Exception):
    pass


async def check_elevenlabs_key(api_key: str) -> None:
    """Refuse a key ElevenLabs will not accept. A key limited to text-to-speech cannot read the account or the
    voice list (403), but it can speak, so only a 401 means the key itself is wrong."""
    key = (api_key or "").strip()
    if not key:
        raise SpeechProviderError("Enter your ElevenLabs API key.")
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            for path in ("/v1/user", "/v1/voices", "/v1/models"):
                response = await client.get(ELEVENLABS_API + path, headers={"xi-api-key": key})
                if response.status_code == 200:
                    return
                if response.status_code == 401:
                    raise SpeechProviderError("ElevenLabs did not accept this key. Check that it is correct.")
    except httpx.HTTPError as exc:
        raise SpeechProviderError(f"Could not reach ElevenLabs: {exc}") from exc
    # Every request was refused for permissions, not for the key: a restricted key. It can still speak.


async def elevenlabs_voices(api_key: str) -> list[dict]:
    """The workspace's voices (its library plus the ElevenLabs defaults), for the voice picker."""
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(
                f"{ELEVENLABS_API}/v1/voices", headers={"xi-api-key": api_key.strip()}
            )
    except httpx.HTTPError as exc:
        raise SpeechProviderError(f"Could not reach ElevenLabs: {exc}") from exc
    if response.status_code in (401, 403):
        raise SpeechProviderError(
            "This ElevenLabs key cannot list voices. Paste a voice ID instead, or give the key the voices_read permission."
        )
    if response.status_code >= 400:
        raise SpeechProviderError(f"ElevenLabs returned an error ({response.status_code}).")
    voices = response.json().get("voices") or []
    return [
        {
            "voice_id": v.get("voice_id"),
            "name": v.get("name"),
            "category": v.get("category"),
            "labels": v.get("labels") or {},
            "preview_url": v.get("preview_url"),
        }
        for v in voices
        if v.get("voice_id")
    ]


async def check_deepgram_key(api_key: str) -> None:
    key = (api_key or "").strip()
    if not key:
        raise SpeechProviderError("Enter your Deepgram API key.")
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(f"{DEEPGRAM_API}/v1/auth/token", headers={"Authorization": f"Token {key}"})
    except httpx.HTTPError as exc:
        raise SpeechProviderError(f"Could not reach Deepgram: {exc}") from exc
    if response.status_code in (401, 403):
        raise SpeechProviderError("Deepgram did not accept this key. Check that it is correct.")
    if response.status_code >= 400:
        raise SpeechProviderError(f"Deepgram returned an error ({response.status_code}).")


# --- voice cloning (ElevenLabs instant voice clone) -----------------------------------------------------------


def _eleven_error(response: httpx.Response) -> str:
    """A reason a visitor can act on, from ElevenLabs' error body."""
    try:
        detail = response.json().get("detail")
    except ValueError:
        detail = None
    status = detail.get("status") if isinstance(detail, dict) else None
    message = detail.get("message") if isinstance(detail, dict) else (detail if isinstance(detail, str) else None)
    if response.status_code == 401:
        return "ElevenLabs did not accept the key. Check the ElevenLabs account under Integrations."
    if status in {"can_not_use_instant_voice_cloning", "voice_limit_reached"} or response.status_code == 402:
        return (
            "This ElevenLabs account cannot make another cloned voice. Instant voice cloning needs a paid plan, "
            "and each plan has a limit on voices. Delete a voice you do not use, or upgrade the plan."
        )
    if status in {"missing_permissions"} or response.status_code == 403:
        return "This ElevenLabs key is not allowed to create voices. Give it the voices (write) permission."
    return f"ElevenLabs could not make this voice: {str(message or response.text)[:240]}"


async def create_instant_clone(
    api_key: str,
    *,
    name: str,
    audio: bytes,
    filename: str,
    content_type: str,
    description: str | None = None,
    labels: dict[str, str] | None = None,
    remove_background_noise: bool = False,
) -> tuple[str, bool]:
    """Make an instant voice clone from one recording. Returns (voice_id, requires_verification)."""
    import json

    data = {"name": name, "remove_background_noise": "true" if remove_background_noise else "false"}
    if description:
        data["description"] = description
    if labels:
        data["labels"] = json.dumps(labels)
    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            response = await client.post(
                f"{ELEVENLABS_API}/v1/voices/add",
                headers={"xi-api-key": api_key.strip()},
                data=data,
                files=[("files", (filename, audio, content_type))],
            )
    except httpx.HTTPError as exc:
        raise SpeechProviderError(f"Could not reach ElevenLabs: {exc}") from exc
    if response.status_code >= 400:
        raise SpeechProviderError(_eleven_error(response))
    body = response.json()
    voice_id = str(body.get("voice_id") or "")
    if not voice_id:
        raise SpeechProviderError("ElevenLabs did not return a voice id.")
    return voice_id, bool(body.get("requires_verification"))


async def delete_elevenlabs_voice(api_key: str, voice_id: str) -> None:
    """Remove a voice from the account. A voice that is already gone counts as removed."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.delete(
                f"{ELEVENLABS_API}/v1/voices/{voice_id}", headers={"xi-api-key": api_key.strip()}
            )
    except httpx.HTTPError as exc:
        raise SpeechProviderError(f"Could not reach ElevenLabs: {exc}") from exc
    if response.status_code in (404, 400) and "not" in response.text.lower():
        return
    if response.status_code >= 400:
        raise SpeechProviderError(_eleven_error(response))


async def elevenlabs_preview(api_key: str, voice_id: str, text: str, model: str) -> bytes:
    """A short spoken sample (mp3) in a voice, so it can be heard before it is used on a call."""
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            response = await client.post(
                f"{ELEVENLABS_API}/v1/text-to-speech/{voice_id}?output_format=mp3_44100_64",
                headers={"xi-api-key": api_key.strip(), "Accept": "audio/mpeg"},
                json={"text": text, "model_id": model or "eleven_flash_v2_5"},
            )
    except httpx.HTTPError as exc:
        raise SpeechProviderError(f"Could not reach ElevenLabs: {exc}") from exc
    if response.status_code >= 400:
        raise SpeechProviderError(_eleven_error(response))
    return response.content


# --- voice cloning (Sarvam instant voice cloning, for Indian languages) --------------------------------------

SARVAM_API = "https://api.sarvam.ai"
# Languages a Sarvam cloned voice can speak; the reference recording may be in any of them or a different one.
SARVAM_CLONE_LANGUAGES: dict[str, str] = {
    "as-IN": "Assamese",
    "bn-IN": "Bengali",
    "en-IN": "English (India)",
    "gu-IN": "Gujarati",
    "hi-IN": "Hindi",
    "kn-IN": "Kannada",
    "ml-IN": "Malayalam",
    "mr-IN": "Marathi",
    "od-IN": "Odia",
    "pa-IN": "Punjabi",
    "ta-IN": "Tamil",
    "te-IN": "Telugu",
}
SARVAM_CLONE_PREFIX = "svc-"  # every Sarvam cloned voice id starts with this


def _sarvam_error(response: httpx.Response) -> str:
    """A reason a visitor can act on, from Sarvam's error body."""
    try:
        error = (response.json() or {}).get("error") or {}
    except ValueError:
        error = {}
    message = str(error.get("message") or response.text or "")[:240]
    if response.status_code in (401,) or error.get("code") in {"invalid_api_key_error", "authentication_error"}:
        return "Sarvam did not accept the key. Check the Sarvam account under Integrations."
    if response.status_code == 403:
        return (
            "This Sarvam account cannot make another cloned voice. Voice cloning must be enabled on the account, "
            "and each plan keeps a limited number of voices. Delete a voice you do not use, or ask Sarvam to enable it."
        )
    if response.status_code == 402 or error.get("code") == "insufficient_quota_error":
        return "The Sarvam account is out of credit."
    if response.status_code == 422:
        return f"Sarvam could not use this recording ({message}). Record again: clear speech, one person, no noise."
    return f"Sarvam could not make this voice: {message}"


async def create_sarvam_clone(
    api_key: str, *, name: str, audio: bytes, filename: str, content_type: str, language: str
) -> str:
    """Make a Sarvam cloned voice from one recording. Returns its voice id (svc-...)."""
    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            response = await client.post(
                f"{SARVAM_API}/voices/create",
                headers={"api-subscription-key": api_key.strip()},
                data={"name": name[:100], "language": language},
                files={"file": (filename, audio, content_type)},
            )
    except httpx.HTTPError as exc:
        raise SpeechProviderError(f"Could not reach Sarvam: {exc}") from exc
    if response.status_code >= 400:
        raise SpeechProviderError(_sarvam_error(response))
    voice_id = str(((response.json() or {}).get("data") or {}).get("voice_id") or "")
    if not voice_id:
        raise SpeechProviderError("Sarvam did not return a voice id.")
    return voice_id


async def delete_sarvam_voice(api_key: str, voice_id: str) -> None:
    """Remove a cloned voice. A voice that is already gone counts as removed."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.delete(
                f"{SARVAM_API}/voices/delete/{voice_id}", headers={"api-subscription-key": api_key.strip()}
            )
    except httpx.HTTPError as exc:
        raise SpeechProviderError(f"Could not reach Sarvam: {exc}") from exc
    if response.status_code == 404:
        return
    if response.status_code >= 400:
        raise SpeechProviderError(_sarvam_error(response))


async def sarvam_clone_preview(api_key: str, voice_id: str, text: str, language: str | None) -> bytes:
    """A short spoken sample (mp3) in a cloned voice."""
    import base64

    data = {"voice_id": voice_id, "text": text[:1000], "output_audio_codec": "mp3"}
    if language:
        data["language_code"] = language
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{SARVAM_API}/voices/clone", headers={"api-subscription-key": api_key.strip()}, files={k: (None, v) for k, v in data.items()}
            )
    except httpx.HTTPError as exc:
        raise SpeechProviderError(f"Could not reach Sarvam: {exc}") from exc
    if response.status_code >= 400:
        raise SpeechProviderError(_sarvam_error(response))
    body = response.json() or {}
    encoded = body.get("audio_b64") or body.get("audio")
    if not encoded:
        raise SpeechProviderError("Sarvam did not return any audio.")
    return base64.b64decode(encoded)
