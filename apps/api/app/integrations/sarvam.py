"""Client for Sarvam's chat/completions API — the "brain" of an AI agent.

OpenAI-compatible per Sarvam's docs (docs.sarvam.ai): POST /v1/chat/completions
with a `messages` array and an optional `tools` array, Bearer auth. Model
`sarvam-105b` is the one confirmed to support tool/function calling (the
`sarvam-105b-conversations` variant is tuned for voice but tool-calling
support on it wasn't confirmed against live docs — stick with `sarvam-105b`
until that's verified). Update 2026-10-03: verified live — sarvam-105b-conversations
returns OpenAI-style tool_calls; live calls use it (see SARVAM_FAST_CHAT_MODEL). The exact `tool_calls` response shape is assumed to
follow the OpenAI convention since Sarvam explicitly targets OpenAI-tooling
compatibility, but this hasn't been exercised against a live response —
verify with a real API key before relying on it in production.

`api_key` is optional per call — omit it to use the platform's shared key,
or pass a tenant's own (BYO) key resolved via
app/integrations/credentials.get_sarvam_api_key. Callers that know which
tenant they're acting for should always pass it explicitly.
"""

import json
import logging
from collections.abc import AsyncIterator

import httpx
from app.core.config import get_settings

logger = logging.getLogger(__name__)

SARVAM_CHAT_URL = "https://api.sarvam.ai/v1/chat/completions"
SARVAM_CHAT_MODEL = "sarvam-105b"
# Sarvam's conversation-tuned variant: no hidden reasoning, so a reply takes ~0.3s
# instead of 2-24s, and tool calls work (checked live 2026-10-03). sarvam-105b
# ignores reasoning_effort="low" in practice, so "low" now means "use this model".
SARVAM_FAST_CHAT_MODEL = "sarvam-105b-conversations"
FAST_MODEL_TIMEOUT = 12.0


# Every language Sarvam's STT (saaras) and TTS (bulbul:v3) accept as language_code.
SARVAM_LANGUAGES: dict[str, str] = {
    "en-IN": "English",
    "hi-IN": "Hindi",
    "bn-IN": "Bengali",
    "gu-IN": "Gujarati",
    "kn-IN": "Kannada",
    "ml-IN": "Malayalam",
    "mr-IN": "Marathi",
    "od-IN": "Odia",
    "pa-IN": "Punjabi",
    "ta-IN": "Tamil",
    "te-IN": "Telugu",
}


class SarvamAPIError(Exception):
    pass


def _has_answer(response: httpx.Response) -> bool:
    """A 200 with neither text nor a tool call is as good as a failure."""
    try:
        message = response.json()["choices"][0]["message"]
    except (ValueError, KeyError, IndexError, TypeError):
        return False
    return bool((message.get("content") or "").strip() or message.get("tool_calls"))


# One pooled client for the process: a fresh AsyncClient per call paid a new TCP + TLS
# handshake to Sarvam on every conversational turn.
_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=30.0,
            limits=httpx.Limits(max_keepalive_connections=20, keepalive_expiry=60.0),
        )
    return _client


async def warm_connection() -> None:
    """Keeps a TLS connection to Sarvam open in the pool. The pool drops idle connections after
    `keepalive_expiry`, so the first turn after a quiet minute paid a fresh TCP + TLS handshake
    (~150-300ms on a live call). Any response, even a 404, proves the connection is alive."""
    try:
        await _http().get("https://api.sarvam.ai/", timeout=5.0)
    except httpx.HTTPError:
        logger.debug("sarvam warm-up request failed", exc_info=True)


async def chat_completion(
    *,
    messages: list[dict],
    tools: list[dict] | None = None,
    temperature: float = 0.3,
    api_key: str | None = None,
    max_tokens: int | None = None,
    timeout: float = 30.0,
    reasoning_effort: str | None = None,
) -> dict:
    key = api_key if api_key is not None else get_settings().sarvam_api_key
    if not key:
        raise SarvamAPIError("No Sarvam API key configured for this tenant or the platform")

    payload: dict = {"model": SARVAM_CHAT_MODEL, "messages": messages, "temperature": temperature}
    if tools:
        payload["tools"] = tools
    if max_tokens:
        # Includes the model's reasoning tokens; long JSON answers need headroom.
        payload["max_tokens"] = max_tokens

    async def post(body: dict, *, wait: float = timeout) -> httpx.Response:
        return await _http().post(
            SARVAM_CHAT_URL, headers={"Authorization": f"Bearer {key}"}, json=body, timeout=wait
        )

    response: httpx.Response | None = None
    if reasoning_effort == "low":
        # Live-call speed. If the fast model fails or stalls, fall back to the
        # reasoning model rather than fail the caller's turn.
        try:
            fast = await post({**payload, "model": SARVAM_FAST_CHAT_MODEL}, wait=min(timeout, FAST_MODEL_TIMEOUT))
            if fast.status_code < 400 and _has_answer(fast):
                response = fast
            elif fast.status_code in (401, 403):
                response = fast  # a bad key fails the same way on any model
        except httpx.TimeoutException:
            response = None
    elif reasoning_effort:
        response = await post({**payload, "reasoning_effort": reasoning_effort})
        if response.status_code in (400, 422):
            response = None
    if response is None:
        if reasoning_effort == "low" and not max_tokens:
            payload["max_tokens"] = 4096  # the reasoning model's default 2048 can run out before it answers
        response = await post(payload)
    if response.status_code >= 400:
        raise SarvamAPIError(
            f"Sarvam chat completion failed ({response.status_code}): {response.text}"
        )
    return response.json()


SARVAM_STT_URL = "https://api.sarvam.ai/speech-to-text"


async def transcribe_audio(audio: bytes, mime_type: str, *, api_key: str | None = None) -> str:
    """Speech to text for a recorded clip (a WhatsApp voice note is OGG/Opus).
    The language is detected automatically. Checked live: ~0.4s for a short note."""
    key = api_key if api_key is not None else get_settings().sarvam_api_key
    if not key:
        raise SarvamAPIError("No Sarvam API key configured for this tenant or the platform")
    extension = {"audio/ogg": "ogg", "audio/mpeg": "mp3", "audio/mp4": "m4a", "audio/aac": "aac", "audio/amr": "amr", "audio/wav": "wav"}
    name = "voice." + extension.get(mime_type.split(";")[0].strip(), "ogg")
    try:
        response = await _http().post(
            SARVAM_STT_URL,
            headers={"api-subscription-key": key},
            files={"file": (name, audio, mime_type.split(";")[0].strip() or "audio/ogg")},
            data={"model": "saarika:v2.5", "language_code": "unknown"},
            timeout=60.0,
        )
    except httpx.HTTPError as exc:
        raise SarvamAPIError(f"Sarvam speech-to-text failed: {exc}") from exc
    if response.status_code >= 400:
        raise SarvamAPIError(f"Sarvam speech-to-text failed ({response.status_code}): {response.text[:300]}")
    return str(response.json().get("transcript") or "").strip()


async def chat_completion_stream(
    *,
    messages: list[dict],
    tools: list[dict] | None = None,
    temperature: float = 0.3,
    api_key: str | None = None,
    reasoning_effort: str | None = None,
    timeout: float = 30.0,
) -> AsyncIterator[dict]:
    """Streaming counterpart of chat_completion for live calls: yields
    {"type": "content", "text": ...} as tokens arrive, then one
    {"type": "final", "content": full text, "tool_calls": [...], "usage": {...}}
    shaped exactly like the non-streaming message so callers can reuse it.

    Sarvam streams OpenAI-style server-sent events (verified live 2026-10-15):
    `delta.content` tokens, `delta.tool_calls` fragments keyed by `index` (id and
    name on the first fragment, `arguments` split across the rest) and a
    `[DONE]` sentinel. If the stream can't be opened, or fails before a single
    token, it falls back to one ordinary request, so a streaming problem never
    costs the caller their reply. A failure after tokens were delivered is
    raised as SarvamAPIError, since those words were already spoken."""
    key = api_key if api_key is not None else get_settings().sarvam_api_key
    if not key:
        raise SarvamAPIError("No Sarvam API key configured for this tenant or the platform")

    fast = reasoning_effort == "low"
    payload: dict = {
        "model": SARVAM_FAST_CHAT_MODEL if fast else SARVAM_CHAT_MODEL,
        "messages": messages,
        "temperature": temperature,
        "stream": True,
    }
    if tools:
        payload["tools"] = tools
    wait = min(timeout, FAST_MODEL_TIMEOUT) if fast else timeout

    delivered = False
    content_parts: list[str] = []
    tool_calls: dict[int, dict] = {}
    usage: dict = {}
    try:
        async with _http().stream(
            "POST",
            SARVAM_CHAT_URL,
            headers={"Authorization": f"Bearer {key}"},
            json=payload,
            timeout=httpx.Timeout(timeout, read=wait),
        ) as response:
            if response.status_code >= 400:
                body = (await response.aread()).decode("utf-8", "replace")
                if response.status_code in (401, 403):
                    raise SarvamAPIError(f"Sarvam chat completion failed ({response.status_code}): {body}")
                raise _StreamUnavailable(f"HTTP {response.status_code}: {body[:200]}")
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except ValueError:
                    continue
                if chunk.get("usage"):
                    usage = chunk["usage"]
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    text = delta.get("content")
                    if text:
                        delivered = True
                        content_parts.append(text)
                        yield {"type": "content", "text": text}
                    for fragment in delta.get("tool_calls") or []:
                        delivered = True
                        slot = tool_calls.setdefault(
                            fragment.get("index", 0),
                            {"id": None, "type": "function", "function": {"name": "", "arguments": ""}},
                        )
                        if fragment.get("id"):
                            slot["id"] = fragment["id"]
                        function = fragment.get("function") or {}
                        if function.get("name"):
                            slot["function"]["name"] += function["name"]
                        if function.get("arguments"):
                            slot["function"]["arguments"] += function["arguments"]
    except SarvamAPIError:
        raise
    except (httpx.HTTPError, _StreamUnavailable) as exc:
        if delivered:
            raise SarvamAPIError(f"Sarvam stream interrupted: {exc}") from exc
        logger.warning("Sarvam stream unavailable (%s); using a normal request", exc)
        completion = await chat_completion(
            messages=messages,
            tools=tools,
            temperature=temperature,
            api_key=key,
            reasoning_effort=reasoning_effort,
            timeout=timeout,
        )
        message = completion["choices"][0]["message"]
        text = message.get("content") or ""
        if text:
            yield {"type": "content", "text": text}
        yield {
            "type": "final",
            "content": text,
            "tool_calls": message.get("tool_calls") or [],
            "usage": completion.get("usage") or {},
        }
        return

    ordered = [tool_calls[i] for i in sorted(tool_calls)]
    for i, call in enumerate(ordered):
        call["id"] = call["id"] or f"call_{i}"
    yield {
        "type": "final",
        "content": "".join(content_parts),
        "tool_calls": ordered,
        "usage": usage,
    }


class _StreamUnavailable(Exception):
    pass
