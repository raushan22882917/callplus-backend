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
