"""Bring-your-own providers for the rest of the world: Deepgram to hear, ElevenLabs to speak, and any
OpenAI-compatible model to write the replies. India stays on Sarvam."""

import json
from datetime import UTC, datetime

import httpx
import pytest
from app.core.config import get_settings
from app.integrations import sarvam
from app.integrations.credentials import get_llm_key, speech_settings
from app.integrations.llm import LlmEndpoint, LlmError, endpoint_from
from app.integrations.providers import language_ok
from app.services.agent_engine import voice_rules_for
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import AIAgent, Call, CallDirection, CallStatus

from .test_channels import _signup


def _auth(headers: dict) -> dict:
    return headers


async def _connect_all(client: AsyncClient, auth: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    async def ok(*_a, **_k):
        return None

    monkeypatch.setattr("app.api.v1.integrations.check_elevenlabs_key", ok)
    monkeypatch.setattr("app.api.v1.integrations.check_deepgram_key", ok)
    assert (await client.post("/api/v1/integrations/elevenlabs", headers=auth, json={"api_key": "xi-key"})).status_code == 201
    assert (await client.post("/api/v1/integrations/deepgram", headers=auth, json={"api_key": "dg-key"})).status_code == 201


# ------------------------------------------------------------------ pure pieces


def test_language_rules() -> None:
    assert language_ok("hi-IN", stt="sarvam", tts="sarvam")
    assert not language_ok("es", stt="sarvam", tts="sarvam")  # Sarvam cannot say Spanish
    assert language_ok("es", stt="deepgram", tts="elevenlabs")
    assert language_ok("hi-IN", stt="deepgram", tts="elevenlabs")  # an Indian language works with the other providers too
    assert not language_ok("xx", stt="deepgram", tts="elevenlabs")


def test_llm_presets_and_validation() -> None:
    ep = endpoint_from("openai", api_key=" sk-1 ", model=None, base_url=None)
    assert ep.url.startswith("https://api.openai.com/") and ep.model and ep.api_key == "sk-1"
    assert endpoint_from("custom", api_key="k", model="m", base_url="https://llm.example.com/v1/chat/completions").model == "m"
    with pytest.raises(LlmError):
        endpoint_from("custom", api_key="k", model="m", base_url=None)  # no address
    with pytest.raises(LlmError):
        endpoint_from("custom", api_key="k", model="m", base_url="http://insecure.example.com")
    with pytest.raises(LlmError):
        endpoint_from("nope", api_key="k", model=None, base_url=None)


def test_international_agents_get_international_rules() -> None:
    india = AIAgent(language="hi-IN", voice="priya", stt_provider="sarvam", tts_provider="sarvam")
    world = AIAgent(language="es", voice="abc", stt_provider="deepgram", tts_provider="elevenlabs")
    assert "Devanagari" in voice_rules_for(india) and "India" in voice_rules_for(india)
    rules = voice_rules_for(world)
    assert "Devanagari" not in rules and "India" not in rules and "Spanish" in rules


# ------------------------------------------------------------------ the language model


def _mock_http(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(sarvam, "_http", lambda: client)


async def test_chat_completion_goes_to_the_workspace_model(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(url=str(request.url), auth=request.headers["authorization"], body=json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "Hello there"}}]})

    _mock_http(monkeypatch, handler)
    ep = LlmEndpoint(url="https://api.openai.com/v1/chat/completions", model="gpt-4.1-mini", api_key="sk-x", label="OpenAI")
    out = await sarvam.chat_completion(
        messages=[{"role": "user", "content": "hi"}], tools=[{"type": "function"}], api_key=ep, reasoning_effort="low"
    )
    assert out["choices"][0]["message"]["content"] == "Hello there"
    assert seen["url"] == ep.url and seen["auth"] == "Bearer sk-x"
    assert seen["body"]["model"] == "gpt-4.1-mini" and "reasoning_effort" not in seen["body"]
    assert seen["body"]["tools"] == [{"type": "function"}]


async def test_chat_completion_errors_name_the_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_http(monkeypatch, lambda r: httpx.Response(401, text="bad key"))
    ep = LlmEndpoint(url="https://x.test/v1/chat/completions", model="m", api_key="k", label="Groq")
    with pytest.raises(sarvam.SarvamAPIError, match="Groq"):
        await sarvam.chat_completion(messages=[{"role": "user", "content": "hi"}], api_key=ep)


async def test_streaming_uses_the_workspace_model(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}
    sse = (
        'data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"lo."}}]}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(url=str(request.url), body=json.loads(request.content))
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

    _mock_http(monkeypatch, handler)
    ep = LlmEndpoint(url="https://api.openai.com/v1/chat/completions", model="gpt-4.1-mini", api_key="sk-x", label="OpenAI")
    events = [e async for e in sarvam.chat_completion_stream(messages=[{"role": "user", "content": "hi"}], api_key=ep, reasoning_effort="low")]
    assert "".join(e["text"] for e in events if e["type"] == "content") == "Hello."
    assert events[-1]["type"] == "final" and events[-1]["content"] == "Hello."
    assert seen["url"] == ep.url and seen["body"]["model"] == "gpt-4.1-mini" and seen["body"]["stream"] is True


# ------------------------------------------------------------------ connecting and choosing


async def test_connect_llm_checks_the_key_before_saving(client: AsyncClient, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession) -> None:
    auth = await _signup(client, slug="llm-co")

    async def refuse(_endpoint):
        raise LlmError("OpenAI did not accept this key.")

    monkeypatch.setattr("app.api.v1.integrations.check_endpoint", refuse)
    bad = await client.post("/api/v1/integrations/llm", headers=auth, json={"preset": "openai", "api_key": "wrong"})
    assert bad.status_code == 422 and "did not accept" in bad.json()["detail"]

    async def accept(_endpoint):
        return None

    monkeypatch.setattr("app.api.v1.integrations.check_endpoint", accept)
    ok = await client.post("/api/v1/integrations/llm", headers=auth, json={"preset": "openai", "api_key": "sk-good", "model": "gpt-4.1"})
    assert ok.status_code == 201 and ok.json()["config"]["model"] == "gpt-4.1"
    assert "sk-good" not in ok.text  # the key is never returned

    catalogue = (await client.get("/api/v1/integrations/providers", headers=auth)).json()
    assert catalogue["llm"] == {"connected": True, "label": "OpenAI", "model": "gpt-4.1"}
    assert "es" in catalogue["languages"]["international"] and "hi-IN" in catalogue["languages"]["indian"]
    assert {p["id"]: p["connected"] for p in catalogue["tts"]} == {"sarvam": True, "elevenlabs": False}

    from app.core.deps import RequestContext  # noqa: F401  (tenant id comes from the agent below)
    me = (await client.get("/api/v1/tenants/me", headers=auth)).json()
    import uuid

    key = await get_llm_key(db_session, uuid.UUID(me["id"]))
    assert isinstance(key, LlmEndpoint) and key.model == "gpt-4.1" and key.api_key == "sk-good"


async def test_agent_needs_the_provider_connected_a_voice_and_a_matching_language(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth = await _signup(client, slug="intl-co")
    wish = {"name": "Spanish line", "language": "es", "stt_provider": "deepgram", "tts_provider": "elevenlabs", "voice": "voice123"}

    # Nothing connected yet.
    r = await client.post("/api/v1/ai-agents", headers=auth, json=wish)
    assert r.status_code == 422 and "Connect your" in r.json()["detail"]

    await _connect_all(client, auth, monkeypatch)
    # ElevenLabs needs a voice.
    r = await client.post("/api/v1/ai-agents", headers=auth, json={**wish, "voice": None})
    assert r.status_code == 422 and "voice" in r.json()["detail"].lower()
    # Sarvam cannot speak Spanish.
    r = await client.post("/api/v1/ai-agents", headers=auth, json={"name": "Bad", "language": "es"})
    assert r.status_code == 422
    # The right combination works and is stored.
    r = await client.post("/api/v1/ai-agents", headers=auth, json=wish)
    assert r.status_code == 201, r.text
    assert (r.json()["stt_provider"], r.json()["tts_provider"], r.json()["voice"]) == ("deepgram", "elevenlabs", "voice123")
    # And it can be switched back to Sarvam for an Indian language.
    back = await client.patch(
        f"/api/v1/ai-agents/{r.json()['id']}", headers=auth, json={"language": "hi-IN", "stt_provider": "sarvam", "tts_provider": "sarvam", "voice": "priya"}
    )
    assert back.status_code == 200 and back.json()["tts_provider"] == "sarvam"


async def test_the_gateway_is_told_which_providers_to_use(client: AsyncClient, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession) -> None:
    auth = await _signup(client, slug="gateway-co")
    await _connect_all(client, auth, monkeypatch)
    agent = (await client.post("/api/v1/ai-agents", headers=auth, json={"name": "Line", "language": "fr", "stt_provider": "deepgram", "tts_provider": "elevenlabs", "voice": "v-fr"})).json()
    tenant_id = (await client.get("/api/v1/tenants/me", headers=auth)).json()["id"]
    call = Call(
        tenant_id=tenant_id, ai_agent_id=agent["id"], direction=CallDirection.INBOUND, status=CallStatus.RINGING,
        from_number="web", to_number="web", started_at=datetime.now(UTC),
    )
    db_session.add(call)
    await db_session.commit()

    resp = await client.get(f"/api/v1/internal/calls/{call.id}/config", headers={"X-Internal-Token": get_settings().internal_service_token})
    assert resp.status_code == 200, resp.text
    cfg = resp.json()
    assert (cfg["stt_provider"], cfg["tts_provider"]) == ("deepgram", "elevenlabs")
    assert (cfg["stt_api_key"], cfg["tts_api_key"]) == ("dg-key", "xi-key")
    assert cfg["voice"] == "v-fr" and cfg["language"] == "fr" and cfg["stt_language"] == "fr"
    assert cfg["tts_model"] == "eleven_flash_v2_5"

    # Disconnecting a provider must never leave a call silent: it falls back to Sarvam.
    assert (await client.delete("/api/v1/integrations/elevenlabs", headers=auth)).status_code == 204
    import uuid

    agent_row = await db_session.get(AIAgent, uuid.UUID(agent["id"]))
    settings = await speech_settings(db_session, uuid.UUID(tenant_id), agent_row)
    assert settings["tts_provider"] == "sarvam" and settings["stt_provider"] == "deepgram"
