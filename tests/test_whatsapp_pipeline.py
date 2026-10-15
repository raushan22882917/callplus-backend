"""WhatsApp end to end: de-duplication, voice notes, human takeover, delivery
receipts, choosing the AI agent and replying from the right number."""

import hashlib
import hmac
import json
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import whatsapp_inbound as wa
from packages.database.models import Message, MessageDirection, WhatsAppAccount


async def _signup(client: AsyncClient, slug: str) -> dict:
    resp = await client.post(
        "/api/v1/auth/signup",
        json={
            "company_name": slug.title(),
            "tenant_slug": slug,
            "full_name": "Owner",
            "email": f"owner@{slug}.com",
            "password": "supersecret1",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _auth(tokens: dict) -> dict:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(b"test-meta-app-secret", body, hashlib.sha256).hexdigest()


def _payload(phone_number_id: str, *messages: dict, statuses: list | None = None) -> bytes:
    value: dict = {"metadata": {"phone_number_id": phone_number_id}, "messages": list(messages)}
    if statuses:
        value["statuses"] = statuses
    return json.dumps({"entry": [{"changes": [{"value": value}]}]}).encode()


async def _post(client: AsyncClient, body: bytes):
    resp = await client.post(
        "/api/v1/webhooks/whatsapp/meta",
        content=body,
        headers={"X-Hub-Signature-256": _sign(body), "Content-Type": "application/json"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch) -> dict:
    """No network: the model, the WhatsApp send/read calls and Redis are faked."""
    state: dict = {"llm": 0, "sent": [], "seen": set(), "prompts": []}

    async def fake_chat_completion(*, messages, tools=None, api_key=None, **kwargs):
        state["llm"] += 1
        state["prompts"].append(messages)
        return {"choices": [{"message": {"role": "assistant", "content": f"Reply {state['llm']}"}}]}

    async def fake_send(account, *, to, body):
        state["sent"].append({"from": account.phone_number_id, "to": to, "body": body})
        return {"messages": [{"id": f"wamid.out{len(state['sent'])}"}]}

    async def fake_mark_read(account, message_id, *, typing=True):
        return None

    async def fake_claim(message_id):
        if message_id in state["seen"]:
            return False
        state["seen"].add(message_id)
        return True

    monkeypatch.setattr("app.services.agent_engine.chat_completion", fake_chat_completion)
    monkeypatch.setattr("app.services.channels.send_text_message", fake_send)
    monkeypatch.setattr("app.services.whatsapp_inbound.mark_read", fake_mark_read)
    monkeypatch.setattr("app.services.whatsapp_inbound.claim", fake_claim)
    return state


async def _setup(client: AsyncClient, slug: str, number_id: str = "1111") -> tuple[dict, str]:
    tokens = await _signup(client, slug)
    agent = await client.post("/api/v1/ai-agents", headers=_auth(tokens), json={"name": "Desk"})
    assert agent.status_code == 201, agent.text
    acct = await client.post(
        "/api/v1/whatsapp-accounts",
        headers=_auth(tokens),
        json={"phone_number_id": number_id, "waba_id": "w1", "display_phone_number": "+15550001", "access_token": "t"},
    )
    assert acct.status_code == 201, acct.text
    # With a single agent the number is answered by it automatically.
    assert acct.json()["ai_agent_id"] == agent.json()["id"]
    return tokens, acct.json()["id"]


def test_parse_webhook_reads_every_message_kind_and_receipts():
    body = {
        "entry": [{"changes": [{"value": {
            "metadata": {"phone_number_id": "1"},
            "contacts": [{"wa_id": "9199", "profile": {"name": "Roshan"}}],
            "messages": [
                {"id": "a", "from": "9199", "type": "text", "text": {"body": "hi"}},
                {"id": "b", "from": "9199", "type": "interactive", "interactive": {"button_reply": {"title": "Book now"}}},
                {"id": "c", "from": "9199", "type": "audio", "audio": {"id": "m1", "mime_type": "audio/ogg; codecs=opus"}},
                {"id": "d", "from": "9199", "type": "image", "image": {"id": "m2", "caption": "my hair"}},
                {"id": "e", "from": "9199", "type": "location", "location": {"name": "Pune"}},
                {"id": "f", "from": "9199", "type": "reaction", "reaction": {"emoji": "👍"}},
            ],
            "statuses": [
                {"id": "out1", "status": "failed", "errors": [{"code": 131047, "title": "Re-engagement message"}]},
                {"id": "out2", "status": "read"},
            ],
        }}]}]
    }
    messages, statuses = wa.parse_webhook(body)
    assert [(m.kind, m.text) for m in messages] == [
        ("text", "hi"),
        ("interactive", "Book now"),
        ("audio", "[Voice note]"),
        ("image", "[Photo] my hair"),
        ("location", "[Location] Pune"),
    ]  # the reaction needs no answer
    assert messages[0].profile_name == "Roshan" and messages[2].media_id == "m1"
    assert statuses[0].status == "failed" and "24 hours" in statuses[0].error
    assert statuses[1].status == "read" and statuses[1].error is None


async def test_meta_resending_a_message_gets_one_reply(client: AsyncClient, fakes: dict) -> None:
    await _setup(client, "wa-dedupe")
    body = _payload("1111", {"id": "wamid.1", "from": "919000000001", "type": "text", "text": {"body": "Hello"}})
    first = await _post(client, body)
    second = await _post(client, body)
    assert first["queued"] == 1 and second["queued"] == 0
    assert fakes["llm"] == 1 and len(fakes["sent"]) == 1


async def test_database_check_catches_a_repeat_when_redis_is_down(client: AsyncClient, fakes: dict, monkeypatch) -> None:
    await _setup(client, "wa-dedupe-db")

    async def redis_down(message_id):
        return True  # what claim() returns when Redis is unavailable

    monkeypatch.setattr("app.services.whatsapp_inbound.claim", redis_down)
    body = _payload("1111", {"id": "wamid.2", "from": "919000000002", "type": "text", "text": {"body": "Hello"}})
    await _post(client, body)
    await _post(client, body)
    assert fakes["llm"] == 1 and len(fakes["sent"]) == 1


async def test_voice_note_is_transcribed_and_answered(client: AsyncClient, db_session: AsyncSession, fakes: dict, monkeypatch) -> None:
    await _setup(client, "wa-voice")

    async def fake_download(account, media_id):
        assert media_id == "media-1"
        return b"OggS...", "audio/ogg"

    async def fake_transcribe(audio, mime, *, api_key=None):
        return "kal do baje haircut book kar do"

    monkeypatch.setattr("app.services.whatsapp_inbound.download_media", fake_download)
    monkeypatch.setattr("app.services.whatsapp_inbound.transcribe_audio", fake_transcribe)
    await _post(client, _payload("1111", {"id": "wamid.v", "from": "919000000003", "type": "audio", "audio": {"id": "media-1", "mime_type": "audio/ogg"}}))

    stored = (await db_session.execute(select(Message).where(Message.provider_message_id == "wamid.v"))).scalar_one()
    assert stored.text == "[Voice note] kal do baje haircut book kar do"
    assert stored.media_url == "whatsapp-media:media-1"
    assert fakes["llm"] == 1 and fakes["sent"][0]["body"] == "Reply 1"
    system_prompt = fakes["prompts"][0][0]["content"]
    assert "[Voice note]" in system_prompt  # the agent is told how to read it


async def test_staff_reply_takes_over_and_hand_back_restores_the_ai(client: AsyncClient, fakes: dict) -> None:
    tokens, _ = await _setup(client, "wa-takeover")
    customer = "919000000004"
    await _post(client, _payload("1111", {"id": "m1", "from": customer, "type": "text", "text": {"body": "Hi"}}))
    assert fakes["llm"] == 1

    inbox = await client.get("/api/v1/channels/inbox", headers=_auth(tokens))
    conversation_id = inbox.json()[0]["id"]
    staff = await client.post(f"/api/v1/channels/conversations/{conversation_id}/reply", headers=_auth(tokens), json={"text": "Hi, this is Asha"})
    assert staff.status_code == 201, staff.text
    thread = await client.get(f"/api/v1/channels/conversations/{conversation_id}", headers=_auth(tokens))
    assert thread.json()["ai_paused_until"] is not None

    await _post(client, _payload("1111", {"id": "m2", "from": customer, "type": "text", "text": {"body": "Thanks Asha"}}))
    assert fakes["llm"] == 1  # AI stayed quiet while staff handle it

    back = await client.post(f"/api/v1/channels/conversations/{conversation_id}/ai", headers=_auth(tokens), json={"active": True})
    assert back.status_code == 200 and back.json()["ai_paused_until"] is None
    await _post(client, _payload("1111", {"id": "m3", "from": customer, "type": "text", "text": {"body": "One more question"}}))
    assert fakes["llm"] == 2


async def test_delivery_receipts_update_sent_messages(client: AsyncClient, db_session: AsyncSession, fakes: dict) -> None:
    await _setup(client, "wa-receipts")
    await _post(client, _payload("1111", {"id": "in1", "from": "919000000005", "type": "text", "text": {"body": "Hi"}}))
    out = (await db_session.execute(select(Message).where(Message.provider_message_id == "wamid.out1"))).scalar_one()
    assert out.direction == MessageDirection.OUTBOUND

    await _post(client, _payload("1111", statuses=[{"id": "wamid.out1", "status": "read"}]))
    await _post(client, _payload("1111", statuses=[{"id": "wamid.out1", "status": "delivered"}]))  # late, ignored
    await db_session.refresh(out)
    assert out.delivery_status == "read"

    await _post(client, _payload("1111", statuses=[{"id": "wamid.out1", "status": "failed", "errors": [{"code": 131026}]}]))
    await db_session.refresh(out)
    assert out.delivery_status == "failed" and "couldn't be delivered" in out.delivery_error


async def test_choose_agent_disconnect_and_reply_from_the_number_written_to(
    client: AsyncClient, db_session: AsyncSession, fakes: dict
) -> None:
    tokens, first_id = await _setup(client, "wa-numbers", number_id="1111")
    second = await client.post(
        "/api/v1/whatsapp-accounts",
        headers=_auth(tokens),
        json={"phone_number_id": "2222", "waba_id": "w2", "display_phone_number": "+15550002", "access_token": "t"},
    )
    second_id = second.json()["id"]

    # Staff only on the first number: messages are stored, no AI reply.
    off = await client.patch(f"/api/v1/whatsapp-accounts/{first_id}", headers=_auth(tokens), json={"ai_agent_id": None})
    assert off.status_code == 200 and off.json()["ai_agent_id"] is None
    await _post(client, _payload("1111", {"id": "n1", "from": "919000000006", "type": "text", "text": {"body": "Hi"}}))
    assert fakes["llm"] == 0

    # The second number answers, and the reply leaves from the second number.
    await _post(client, _payload("2222", {"id": "n2", "from": "919000000007", "type": "text", "text": {"body": "Hi"}}))
    assert fakes["sent"][-1]["from"] == "2222"

    bad = await client.patch(f"/api/v1/whatsapp-accounts/{second_id}", headers=_auth(tokens), json={"ai_agent_id": str(uuid.uuid4())})
    assert bad.status_code == 422

    gone = await client.delete(f"/api/v1/whatsapp-accounts/{first_id}", headers=_auth(tokens))
    assert gone.status_code == 204
    assert await db_session.get(WhatsAppAccount, uuid.UUID(first_id)) is None
