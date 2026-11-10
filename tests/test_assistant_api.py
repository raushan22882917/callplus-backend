import json

import pytest
from httpx import AsyncClient


async def _signup(client: AsyncClient, *, slug: str, email: str) -> dict:
    resp = await client.post(
        "/api/v1/auth/signup",
        json={
            "company_name": slug.title(),
            "tenant_slug": slug,
            "full_name": "Owner",
            "email": email,
            "password": "supersecret1",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _auth(tokens: dict) -> dict:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def _final_completion(content: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


def _tool_call_completion(*, tool_call_id: str, name: str, arguments: dict) -> dict:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": tool_call_id,
                            "function": {"name": name, "arguments": json.dumps(arguments)},
                        }
                    ],
                }
            }
        ]
    }


async def test_chat_answers_general_question_and_persists_history(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_chat_completion(*, messages, tools=None, api_key=None, **kwargs):
        return _final_completion("Hello there, how can I help?")

    monkeypatch.setattr("app.services.agent_engine.chat_completion", fake_chat_completion)

    tokens = await _signup(client, slug="acme-co", email="owner@acme.com")

    first = await client.post(
        "/api/v1/assistant/chat", headers=_auth(tokens), json={"message": "hi there"}
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["reply_text"] == "Hello there, how can I help?"
    conversation_id = body["conversation_id"]

    second = await client.post(
        "/api/v1/assistant/chat",
        headers=_auth(tokens),
        json={"conversation_id": conversation_id, "message": "what's 2+2?"},
    )
    assert second.status_code == 200, second.text
    assert second.json()["conversation_id"] == conversation_id

    conversations = await client.get("/api/v1/assistant/conversations", headers=_auth(tokens))
    assert conversations.status_code == 200
    assert len(conversations.json()) == 1

    messages = await client.get(
        f"/api/v1/assistant/conversations/{conversation_id}/messages", headers=_auth(tokens)
    )
    assert messages.status_code == 200
    texts = [m["text"] for m in messages.json()]
    assert texts == [
        "hi there",
        "Hello there, how can I help?",
        "what's 2+2?",
        "Hello there, how can I help?",
    ]


async def test_chat_can_call_a_registry_tool(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"count": 0}

    async def fake_chat_completion(*, messages, tools=None, api_key=None, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            return _tool_call_completion(
                tool_call_id="call_1", name="get_customer", arguments={"phone": "+15550001111"}
            )
        return _final_completion("Found them, here's a summary.")

    monkeypatch.setattr("app.services.agent_engine.chat_completion", fake_chat_completion)

    tokens = await _signup(client, slug="beta-co", email="owner@beta.com")
    customer_resp = await client.post(
        "/api/v1/customers",
        headers=_auth(tokens),
        json={"name": "Jane Doe", "phone": "+15550001111"},
    )
    assert customer_resp.status_code == 201, customer_resp.text

    resp = await client.post(
        "/api/v1/assistant/chat",
        headers=_auth(tokens),
        json={"message": "look up +15550001111"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["reply_text"] == "Found them, here's a summary."
    assert calls["count"] == 2


async def test_another_tenant_cannot_read_assistant_conversation(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    tokens_a = await _signup(client, slug="tenant-a", email="owner@tenant-a.com")
    tokens_b = await _signup(client, slug="tenant-b", email="owner@tenant-b.com")

    async def fake_chat_completion(*, messages, tools=None, api_key=None, **kwargs):
        return _final_completion("ok")

    monkeypatch.setattr("app.services.agent_engine.chat_completion", fake_chat_completion)

    chat_resp = await client.post(
        "/api/v1/assistant/chat", headers=_auth(tokens_a), json={"message": "hi"}
    )
    assert chat_resp.status_code == 200, chat_resp.text
    conversation_id = chat_resp.json()["conversation_id"]

    as_b = await client.get(
        f"/api/v1/assistant/conversations/{conversation_id}/messages", headers=_auth(tokens_b)
    )
    assert as_b.status_code == 404


async def test_chat_lists_urgent_tickets_with_the_customer(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict = {}

    async def fake_chat_completion(*, messages, tools=None, api_key=None, **kwargs):
        if not seen:
            seen["tools"] = [t["function"]["name"] for t in tools or []]
            return _tool_call_completion(
                tool_call_id="call_1", name="list_open_tickets", arguments={"priority": "urgent"}
            )
        seen["tool"] = next(m["content"] for m in reversed(messages) if m.get("role") == "tool")
        return _final_completion("Asha has one urgent ticket.")

    monkeypatch.setattr("app.services.agent_engine.chat_completion", fake_chat_completion)
    tokens = await _signup(client, slug="queue-co", email="owner@queue.com")
    customer = await client.post(
        "/api/v1/customers",
        headers=_auth(tokens),
        json={"name": "Asha", "phone": "+919800000001"},
    )
    assert customer.status_code == 201, customer.text
    ticket = await client.post(
        "/api/v1/tickets",
        headers=_auth(tokens),
        json={
            "customer_id": customer.json()["id"],
            "subject": "Air fitting",
            "priority": "urgent",
        },
    )
    assert ticket.status_code == 201, ticket.text

    resp = await client.post(
        "/api/v1/assistant/chat",
        headers=_auth(tokens),
        json={
            "message": "Show me all urgent open support tickets and what customers they belong to."
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["reply_text"] == "Asha has one urgent ticket."
    assert "list_open_tickets" in seen["tools"]
    assert "Air fitting" in seen["tool"]
    assert "Asha" in seen["tool"]


async def test_chat_says_to_connect_sarvam_when_the_model_is_down(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.integrations.sarvam import SarvamAPIError

    async def fake_chat_completion(*, messages, tools=None, api_key=None, **kwargs):
        raise SarvamAPIError("Connect your own Sarvam API key under Integrations.")

    monkeypatch.setattr("app.services.agent_engine.chat_completion", fake_chat_completion)

    tokens = await _signup(client, slug="nokey-co", email="owner@nokey.com")
    resp = await client.post(
        "/api/v1/assistant/chat", headers=_auth(tokens), json={"message": "What is open?"}
    )
    assert resp.status_code == 200, resp.text
    assert "Connect your Sarvam key under Integrations" in resp.json()["reply_text"]
    assert "human" not in resp.json()["reply_text"]
