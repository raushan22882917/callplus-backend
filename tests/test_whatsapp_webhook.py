import hashlib
import hmac
import json
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import Ticket, TicketPriority, WhatsAppAccount


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


def _sign(body: bytes) -> str:
    # META_WHATSAPP_APP_SECRET is forced to "" for the whole test run (see
    # tests/conftest.py), so the webhook signature is HMAC-SHA256 with an
    # empty key.
    digest = hmac.new(b"", body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


async def test_whatsapp_transfer_request_raises_urgent_ticket_for_human_agent(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tokens = await _signup(client, slug="whatsapp-co", email="owner@whatsapp.co")

    agent_resp = await client.post(
        "/api/v1/ai-agents", headers=_auth(tokens), json={"name": "Support Bot"}
    )
    assert agent_resp.status_code == 201, agent_resp.text
    agent_id = agent_resp.json()["id"]

    tool_resp = await client.put(
        f"/api/v1/ai-agents/{agent_id}/tools",
        headers=_auth(tokens),
        json={"tool_name": "transfer_call", "enabled": True},
    )
    assert tool_resp.status_code == 200, tool_resp.text

    account_resp = await client.post(
        "/api/v1/whatsapp-accounts",
        headers=_auth(tokens),
        json={
            "phone_number_id": "106540352242922",
            "waba_id": "102290129340398",
            "display_phone_number": "+15550783881",
            "access_token": "fake-token",
        },
    )
    assert account_resp.status_code == 201, account_resp.text

    # No API sets ai_agent_id on a WhatsApp account yet — wire it directly.
    account = await db_session.get(WhatsAppAccount, uuid.UUID(account_resp.json()["id"]))
    account.ai_agent_id = uuid.UUID(agent_id)
    await db_session.commit()

    calls = {"count": 0}

    async def fake_chat_completion(*, messages, tools=None, api_key=None, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            return _tool_call_completion(
                tool_call_id="call_1",
                name="transfer_call",
                arguments={"reason": "customer asked for a human"},
            )
        return _final_completion("I'm connecting you with a teammate now.")

    async def fake_send_text_message(account, *, to, body):
        return {"messages": [{"id": "wamid.fake"}]}

    monkeypatch.setattr("app.services.agent_engine.chat_completion", fake_chat_completion)
    monkeypatch.setattr("app.services.channels.send_text_message", fake_send_text_message)

    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": "106540352242922"},
                            "messages": [
                                {
                                    "from": "15559990000",
                                    "type": "text",
                                    "text": {"body": "I need to talk to a person"},
                                }
                            ],
                        }
                    }
                ]
            }
        ]
    }
    body = json.dumps(payload).encode()

    resp = await client.post(
        "/api/v1/webhooks/whatsapp/meta",
        content=body,
        headers={"X-Hub-Signature-256": _sign(body), "Content-Type": "application/json"},
    )
    assert resp.status_code == 200, resp.text
    assert calls["count"] == 2

    tickets = (
        await db_session.execute(select(Ticket).where(Ticket.tenant_id == account.tenant_id))
    ).scalars().all()
    assert len(tickets) == 1
    assert tickets[0].priority == TicketPriority.URGENT
    assert "human agent" in tickets[0].subject
