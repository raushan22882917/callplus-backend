"""Cloning a voice for an agent: consent, limits, who may use which voice, and deleting."""

import uuid

import pytest
from app.core.config import get_settings
from app.integrations.speech_providers import SpeechProviderError
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import AuditLog, VoiceClone

from .test_channels import _signup

AUDIO = b"RIFF" + b"\x00" * 40_000  # big enough to count as a recording; the provider is faked


def _form(**over) -> dict:
    return {
        "name": "Asha",
        "speaker_name": "Asha Rao",
        "consent": "true",
        "consent_version": "2026-11-v1",
        "duration_seconds": "40",
        **over,
    }


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> dict:
    """A fake ElevenLabs: records what was asked and hands out voice ids."""
    state: dict = {"created": [], "deleted": [], "fail": None, "verify": False}

    async def create(key, **kw):
        if state["fail"]:
            raise SpeechProviderError(state["fail"])
        voice_id = f"clone-{len(state['created']) + 1}"
        state["created"].append({"key": key, "voice_id": voice_id, **kw})
        return voice_id, state["verify"]

    async def delete(key, voice_id):
        state["deleted"].append((key, voice_id))

    async def preview(key, voice_id, text, model):
        state["preview"] = (key, voice_id, text, model)
        return b"ID3-fake-mp3"

    async def check(*_a, **_k):
        return None

    async def sarvam_create(key, **kw):
        if state["fail"]:
            raise SpeechProviderError(state["fail"])
        voice_id = f"svc-{len(state['sarvam_created']) + 1}"
        state["sarvam_created"].append({"key": key, "voice_id": voice_id, **kw})
        return voice_id

    async def sarvam_delete(key, voice_id):
        state["sarvam_deleted"].append((key, voice_id))

    async def sarvam_preview(key, voice_id, text, language):
        state["sarvam_preview"] = (key, voice_id, text, language)
        return b"ID3-sarvam"

    async def check_sarvam(*_a, **_k):
        return None

    state.update(sarvam_created=[], sarvam_deleted=[])
    monkeypatch.setattr("app.api.v1.voice_clones.create_sarvam_clone", sarvam_create)
    monkeypatch.setattr("app.api.v1.voice_clones.delete_sarvam_voice", sarvam_delete)
    monkeypatch.setattr("app.api.v1.voice_clones.sarvam_clone_preview", sarvam_preview)
    monkeypatch.setattr("app.api.v1.integrations.check_api_key", check_sarvam)
    monkeypatch.setattr("app.api.v1.voice_clones.create_instant_clone", create)
    monkeypatch.setattr("app.api.v1.voice_clones.delete_elevenlabs_voice", delete)
    monkeypatch.setattr("app.api.v1.voice_clones.elevenlabs_preview", preview)
    monkeypatch.setattr("app.api.v1.integrations.check_elevenlabs_key", check)
    return state


async def _own_eleven(client: AsyncClient, auth: dict) -> None:
    resp = await client.post("/api/v1/integrations/elevenlabs", headers=auth, json={"api_key": "own-eleven"})
    assert resp.status_code == 201, resp.text


async def _create(client: AsyncClient, auth: dict, **over):
    return await client.post(
        "/api/v1/voice-clones", headers=auth, data=_form(**over), files={"file": ("voice.wav", AUDIO, "audio/wav")}
    )


async def test_the_script_shows_the_consent_and_what_is_allowed(client: AsyncClient, provider: dict) -> None:
    auth = await _signup(client, slug="script-co")
    none = (await client.get("/api/v1/voice-clones/script", headers=auth)).json()
    assert none["available"] is False and none["source"] is None  # no ElevenLabs account yet
    await _own_eleven(client, auth)
    got = (await client.get("/api/v1/voice-clones/script", headers=auth)).json()
    assert got["available"] and got["source"] == "own" and got["limit"] == 25 and got["used"] == 0
    assert "permission" in got["consent_text"] and len(got["reading_passage"]) > 200 and got["version"]


async def test_a_voice_is_cloned_with_consent_listed_previewed_used_and_deleted(
    client: AsyncClient, provider: dict, db_session: AsyncSession
) -> None:
    auth = await _signup(client, slug="clone-co")
    await _own_eleven(client, auth)

    made = await _create(client, auth)
    assert made.status_code == 201, made.text
    body = made.json()
    assert body["provider_voice_id"] == "clone-1" and body["status"] == "ready" and body["key_source"] == "own"
    assert body["speaker_name"] == "Asha Rao" and body["consent_version"] == "2026-11-v1"
    sent = provider["created"][0]
    assert sent["key"] == "own-eleven" and sent["audio"] == AUDIO and sent["name"] == "Asha"

    row = (await db_session.execute(select(VoiceClone).where(VoiceClone.id == uuid.UUID(body["id"])))).scalar_one()
    assert "permission" in row.consent_text and row.consent_at is not None
    audit = (await db_session.execute(select(AuditLog).where(AuditLog.action == "voice_clone.created"))).scalars().all()
    assert audit and audit[0].extra["speaker_name"] == "Asha Rao"

    assert [c["id"] for c in (await client.get("/api/v1/voice-clones", headers=auth)).json()] == [body["id"]]
    heard = await client.post(f"/api/v1/voice-clones/{body['id']}/preview", headers=auth, json={"text": "Hi there"})
    assert heard.status_code == 200 and heard.headers["content-type"] == "audio/mpeg" and heard.content == b"ID3-fake-mp3"
    assert provider["preview"][:3] == ("own-eleven", "clone-1", "Hi there")

    # An agent can speak with it, and while it does the voice cannot be deleted.
    agent = await client.post(
        "/api/v1/ai-agents", headers=auth,
        json={"name": "Line", "language": "en-US", "stt_provider": "sarvam", "tts_provider": "elevenlabs", "voice": "clone-1"},
    )
    assert agent.status_code == 201, agent.text
    blocked = await client.delete(f"/api/v1/voice-clones/{body['id']}", headers=auth)
    assert blocked.status_code == 409 and "Line" in blocked.json()["detail"]
    await client.patch(f"/api/v1/ai-agents/{agent.json()['id']}", headers=auth, json={"tts_provider": "sarvam", "voice": "priya", "language": "en-IN"})
    gone = await client.delete(f"/api/v1/voice-clones/{body['id']}", headers=auth)
    assert gone.status_code == 204 and provider["deleted"] == [("own-eleven", "clone-1")]
    assert (await client.get("/api/v1/voice-clones", headers=auth)).json() == []


async def test_the_speaker_must_agree_and_the_recording_must_be_usable(client: AsyncClient, provider: dict) -> None:
    auth = await _signup(client, slug="rules-co")
    await _own_eleven(client, auth)
    assert (await _create(client, auth, consent="false")).status_code == 422
    assert (await _create(client, auth, consent_version="old")).status_code == 422
    assert (await _create(client, auth, duration_seconds="5")).status_code == 422
    assert (await _create(client, auth, duration_seconds="9999")).status_code == 422
    tiny = await client.post("/api/v1/voice-clones", headers=auth, data=_form(), files={"file": ("v.wav", b"x" * 100, "audio/wav")})
    assert tiny.status_code == 422
    text = await client.post("/api/v1/voice-clones", headers=auth, data=_form(), files={"file": ("notes.txt", AUDIO, "text/plain")})
    assert text.status_code == 422
    assert provider["created"] == []  # nothing reached the provider


async def test_needs_a_provider_account_and_reports_provider_problems(client: AsyncClient, provider: dict) -> None:
    auth = await _signup(client, slug="noacct-co")
    refused = await _create(client, auth)
    assert refused.status_code == 422 and "Connect your ElevenLabs" in refused.json()["detail"]

    await _own_eleven(client, auth)
    provider["fail"] = "This ElevenLabs account cannot make another cloned voice."
    failed = await _create(client, auth)
    assert failed.status_code == 422 and "cannot make another" in failed.json()["detail"]

    provider["fail"] = None
    provider["verify"] = True
    held = await _create(client, auth)
    assert held.status_code == 201 and held.json()["status"] == "needs_verification" and held.json()["requires_verification"]


async def test_the_workspace_limit_on_cloned_voices(client: AsyncClient, provider: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOICE_CLONES_OWN_CAP", "1")
    get_settings.cache_clear()
    try:
        auth = await _signup(client, slug="cap-co")
        await _own_eleven(client, auth)
        assert (await _create(client, auth)).status_code == 201
        second = await _create(client, auth, name="Second")
        assert second.status_code == 422 and "1 cloned voice" in second.json()["detail"]
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


async def test_on_callplus_s_shared_account_one_workspace_cannot_use_anothers_clone(
    client: AsyncClient, provider: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in {"ELEVENLABS_API_KEY": "platform-eleven", "MANAGED_PROVIDERS": "elevenlabs", "SARVAM_API_KEY": "s"}.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()

    async def premade(_key):
        return {"premade-1"}

    async def library(_key):
        return [
            {"voice_id": "premade-1", "name": "Rachel", "category": "premade", "labels": {}, "preview_url": None},
            {"voice_id": "clone-1", "name": "Somebody's clone", "category": "cloned", "labels": {}, "preview_url": None},
        ]

    monkeypatch.setattr("app.services.voice_clones._premade_ids", premade)
    monkeypatch.setattr("app.api.v1.integrations.elevenlabs_voices", library)
    try:
        a = await _signup(client, slug="tenant-a")
        b = await _signup(client, slug="tenant-b")
        made = await _create(client, a)  # on CallPlus's account: "clone-1"
        assert made.status_code == 201 and made.json()["key_source"] == "managed"
        assert provider["created"][0]["key"] == "platform-eleven"

        wish = {"name": "Line", "language": "en-US", "stt_provider": "sarvam", "tts_provider": "elevenlabs"}
        # B may use a premade voice but not A's clone.
        assert (await client.post("/api/v1/ai-agents", headers=b, json={**wish, "voice": "clone-1"})).status_code == 422
        assert (await client.post("/api/v1/ai-agents", headers=b, json={**wish, "voice": "premade-1"})).status_code == 201
        # A may use its own.
        assert (await client.post("/api/v1/ai-agents", headers=a, json={**wish, "voice": "clone-1"})).status_code == 201

        # The voice lists: B sees only premade voices, A sees its clone first.
        b_list = (await client.get("/api/v1/integrations/elevenlabs/voices", headers=b)).json()
        a_list = (await client.get("/api/v1/integrations/elevenlabs/voices", headers=a)).json()
        assert [v["voice_id"] for v in b_list] == ["premade-1"]
        assert [v["voice_id"] for v in a_list] == ["clone-1", "premade-1"] and a_list[0]["name"].endswith("(cloned)")

        # And B cannot see, hear or delete A's clone.
        clone_id = made.json()["id"]
        assert (await client.get("/api/v1/voice-clones", headers=b)).json() == []
        assert (await client.post(f"/api/v1/voice-clones/{clone_id}/preview", headers=b, json={"text": "hi"})).status_code == 404
        assert (await client.delete(f"/api/v1/voice-clones/{clone_id}", headers=b)).status_code == 404
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


async def test_the_shared_account_limit_is_lower(client: AsyncClient, provider: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {"ELEVENLABS_API_KEY": "platform-eleven", "MANAGED_PROVIDERS": "elevenlabs", "SARVAM_API_KEY": "s"}.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    try:
        auth = await _signup(client, slug="shared-cap")
        assert (await _create(client, auth, name="One")).status_code == 201
        assert (await _create(client, auth, name="Two")).status_code == 201
        third = await _create(client, auth, name="Three")
        assert third.status_code == 422 and "2 cloned voices" in third.json()["detail"]
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


# ------------------------------------------------------------------ Sarvam (Indian languages)


async def _own_sarvam(client: AsyncClient, auth: dict) -> None:
    resp = await client.post("/api/v1/integrations/sarvam", headers=auth, json={"api_key": "own-sarvam"})
    assert resp.status_code == 201, resp.text


async def _create_sarvam(client: AsyncClient, auth: dict, language: str = "hi-IN", **over):
    return await client.post(
        "/api/v1/voice-clones", headers=auth, data=_form(provider="sarvam", language=language, **over),
        files={"file": ("voice.wav", AUDIO, "audio/wav")},
    )


async def test_the_sarvam_script_names_sarvam_and_offers_its_languages(client: AsyncClient, provider: dict) -> None:
    auth = await _signup(client, slug="sv-script")
    none = (await client.get("/api/v1/voice-clones/script?provider=sarvam", headers=auth)).json()
    assert none["available"] is False
    await _own_sarvam(client, auth)
    got = (await client.get("/api/v1/voice-clones/script?provider=sarvam", headers=auth)).json()
    assert got["available"] and got["source"] == "own" and "Sarvam AI" in got["consent_text"]
    assert "hi-IN" in got["languages"] and "ur-IN" not in got["languages"]
    eleven = (await client.get("/api/v1/voice-clones/script", headers=auth)).json()
    assert "ElevenLabs" in eleven["consent_text"] and eleven["languages"] is None
    assert (await client.get("/api/v1/voice-clones/script?provider=nope", headers=auth)).status_code == 422


async def test_a_sarvam_voice_is_cloned_used_by_an_agent_and_reaches_the_gateway(
    client: AsyncClient, provider: dict, db_session: AsyncSession
) -> None:
    from datetime import UTC, datetime

    from packages.database.models import Call, CallDirection, CallStatus

    auth = await _signup(client, slug="sv-clone")
    await _own_sarvam(client, auth)

    # It needs the language the voice will speak, from Sarvam's list.
    no_lang = await client.post("/api/v1/voice-clones", headers=auth, data=_form(provider="sarvam"), files={"file": ("v.wav", AUDIO, "audio/wav")})
    assert no_lang.status_code == 422
    assert (await _create_sarvam(client, auth, language="ur-IN")).status_code == 422  # Urdu is not offered

    made = await _create_sarvam(client, auth)
    assert made.status_code == 201, made.text
    body = made.json()
    assert body["provider"] == "sarvam" and body["provider_voice_id"] == "svc-1" and body["language"] == "hi-IN"
    sent = provider["sarvam_created"][0]
    assert sent["key"] == "own-sarvam" and sent["language"] == "hi-IN" and sent["audio"] == AUDIO

    heard = await client.post(f"/api/v1/voice-clones/{body['id']}/preview", headers=auth, json={"text": "Namaste"})
    assert heard.status_code == 200 and heard.content == b"ID3-sarvam"
    assert provider["sarvam_preview"] == ("own-sarvam", "svc-1", "Namaste", "hi-IN")

    agent = await client.post(
        "/api/v1/ai-agents", headers=auth,
        json={"name": "Hindi line", "language": "hi-IN", "stt_provider": "sarvam", "tts_provider": "sarvam", "voice": "svc-1"},
    )
    assert agent.status_code == 201, agent.text
    me = (await client.get("/api/v1/tenants/me", headers=auth)).json()["id"]
    call = Call(
        tenant_id=me, ai_agent_id=agent.json()["id"], direction=CallDirection.INBOUND, status=CallStatus.RINGING,
        from_number="web", to_number="web", started_at=datetime.now(UTC),
    )
    db_session.add(call)
    await db_session.commit()
    cfg = (await client.get(f"/api/v1/internal/calls/{call.id}/config", headers={"X-Internal-Token": get_settings().internal_service_token})).json()
    assert cfg["voice"] == "svc-1" and cfg["tts_provider"] == "sarvam" and cfg["tts_api_key"] == "own-sarvam"

    blocked = await client.delete(f"/api/v1/voice-clones/{body['id']}", headers=auth)
    assert blocked.status_code == 409 and "Hindi line" in blocked.json()["detail"]
    await client.patch(f"/api/v1/ai-agents/{agent.json()['id']}", headers=auth, json={"voice": "priya"})
    assert (await client.delete(f"/api/v1/voice-clones/{body['id']}", headers=auth)).status_code == 204
    assert provider["sarvam_deleted"] == [("own-sarvam", "svc-1")]


async def test_another_workspaces_sarvam_voice_id_is_refused_and_never_spoken(
    client: AsyncClient, provider: dict, db_session: AsyncSession
) -> None:
    a = await _signup(client, slug="sv-a")
    b = await _signup(client, slug="sv-b")
    await _own_sarvam(client, a)
    await _own_sarvam(client, b)
    assert (await _create_sarvam(client, a)).status_code == 201  # svc-1 belongs to A

    wish = {"name": "Line", "language": "hi-IN", "stt_provider": "sarvam", "tts_provider": "sarvam", "voice": "svc-1"}
    refused = await client.post("/api/v1/ai-agents", headers=b, json=wish)
    assert refused.status_code == 422 and "not available" in refused.json()["detail"]

    # Even if it got onto an agent another way, the call would not use it.
    from app.integrations.credentials import speech_settings

    from packages.database.models import AIAgent

    ok = await client.post("/api/v1/ai-agents", headers=b, json={**wish, "voice": "priya"})
    agent = await db_session.get(AIAgent, uuid.UUID(ok.json()["id"]))
    agent.voice = "svc-1"
    await db_session.commit()
    me = uuid.UUID((await client.get("/api/v1/tenants/me", headers=b)).json()["id"])
    assert (await speech_settings(db_session, me, agent))["voice"] == "priya"


async def test_a_voice_made_on_callplus_sarvam_account_still_uses_that_account(
    client: AsyncClient, provider: dict, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.integrations.credentials import speech_settings

    from packages.database.models import AIAgent

    for name, value in {"SARVAM_API_KEY": "platform-sarvam", "MANAGED_PROVIDERS": "sarvam"}.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    try:
        auth = await _signup(client, slug="sv-managed")
        made = await _create_sarvam(client, auth)
        assert made.status_code == 201 and made.json()["key_source"] == "managed"
        assert provider["sarvam_created"][0]["key"] == "platform-sarvam"

        # The workspace now connects its own Sarvam account, which does not hold that voice.
        await _own_sarvam(client, auth)
        agent = (await client.post("/api/v1/ai-agents", headers=auth, json={"name": "L", "language": "hi-IN", "voice": "svc-1"})).json()
        row = await db_session.get(AIAgent, uuid.UUID(agent["id"]))
        me = uuid.UUID((await client.get("/api/v1/tenants/me", headers=auth)).json()["id"])
        speech = await speech_settings(db_session, me, row)
        assert speech["voice"] == "svc-1" and speech["tts_api_key"] == "platform-sarvam"  # the account that holds it
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
