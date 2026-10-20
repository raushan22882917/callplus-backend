"""Verifies today's automation wiring actually fires: every place that
creates a Customer/Ticket, or receives an inbound message, now dispatches
the matching WorkflowTrigger — see app/services/customer_lookup.py and the
call sites in app/api/v1/{calls,telephony,whatsapp,tickets,customers,internal}.py.

Each test monkeypatches `enqueue_workflow_trigger` where the module under
test imported it (not the Celery task itself), so no real Redis/Celery
broker is needed — this matches the existing test suite's convention of
mocking at the call site (see test_whatsapp_webhook.py).
"""

import hashlib
import hmac
import json
import uuid

import pytest
from app.core.config import get_settings
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import (
    Call,
    CallDirection,
    CallStatus,
    Customer,
    Tenant,
    Ticket,
    Workflow,
    WorkflowTrigger,
)


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


def _internal_headers() -> dict:
    return {"X-Internal-Token": get_settings().internal_service_token}


def _capture(monkeypatch: pytest.MonkeyPatch, target: str) -> list[dict]:
    """Patches `enqueue_workflow_trigger` at `target` and returns a list
    that fills up with {trigger, payload} dicts as it's called."""
    calls: list[dict] = []

    def fake_enqueue(*, tenant_id, trigger, payload):
        calls.append({"tenant_id": tenant_id, "trigger": trigger, "payload": payload})

    monkeypatch.setattr(target, fake_enqueue)
    return calls


async def test_manual_customer_creation_fires_customer_created(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _capture(monkeypatch, "app.api.v1.customers.enqueue_workflow_trigger")
    tokens = await _signup(client, slug="manual-cust-co", email="owner@manualcustco.com")

    resp = await client.post(
        "/api/v1/customers",
        headers=_auth(tokens),
        json={"name": "Priya", "phone": "+15551230000"},
    )
    assert resp.status_code == 201, resp.text

    assert len(calls) == 1
    assert calls[0]["trigger"] == WorkflowTrigger.CUSTOMER_CREATED
    assert calls[0]["payload"]["phone"] == "+15551230000"
    assert calls[0]["payload"]["customer_id"] == resp.json()["id"]


async def test_manual_ticket_creation_fires_ticket_created(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.api.v1.customers.enqueue_workflow_trigger", lambda **_: None)
    calls = _capture(monkeypatch, "app.api.v1.tickets.enqueue_workflow_trigger")
    tokens = await _signup(client, slug="manual-ticket-co", email="owner@manualticketco.com")

    cust_resp = await client.post(
        "/api/v1/customers", headers=_auth(tokens), json={"name": "Dev", "phone": "+15557778888"}
    )
    customer_id = cust_resp.json()["id"]

    resp = await client.post(
        "/api/v1/tickets",
        headers=_auth(tokens),
        json={"customer_id": customer_id, "subject": "Billing question", "priority": "high"},
    )
    assert resp.status_code == 201, resp.text

    assert len(calls) == 1
    assert calls[0]["trigger"] == WorkflowTrigger.TICKET_CREATED
    assert calls[0]["payload"]["customer_id"] == customer_id
    assert calls[0]["payload"]["priority"] == "high"


async def test_outbound_call_to_new_number_creates_customer_and_fires_trigger(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.api.v1.calls.place_outbound_call", lambda **_: "CAoutboundfake")
    calls = _capture(monkeypatch, "app.api.v1.calls.enqueue_workflow_trigger")
    tokens = await _signup(client, slug="outbound-co", email="owner@outboundco.com")

    agent_resp = await client.post(
        "/api/v1/ai-agents", headers=_auth(tokens), json={"name": "Sales"}
    )
    agent_id = agent_resp.json()["id"]

    resp = await client.post(
        "/api/v1/calls",
        headers=_auth(tokens),
        json={
            "direction": "outbound",
            "to_number": "+15559990001",
            "from_number": "+15550001234",
            "ai_agent_id": agent_id,
        },
    )
    assert resp.status_code == 201, resp.text
    call_id = resp.json()["id"]

    call = await db_session.get(Call, uuid.UUID(call_id))
    assert call.customer_id is not None
    customer = await db_session.get(Customer, call.customer_id)
    assert customer.phone == "+15559990001"

    assert len(calls) == 1
    assert calls[0]["trigger"] == WorkflowTrigger.CUSTOMER_CREATED
    assert calls[0]["payload"]["customer_id"] == str(customer.id)


async def test_outbound_call_to_known_number_does_not_refire_customer_created(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.api.v1.calls.place_outbound_call", lambda **_: "CAoutboundfake")
    calls = _capture(monkeypatch, "app.api.v1.calls.enqueue_workflow_trigger")
    tokens = await _signup(client, slug="repeat-caller-co", email="owner@repeatcallerco.com")

    agent_resp = await client.post(
        "/api/v1/ai-agents", headers=_auth(tokens), json={"name": "Sales"}
    )
    agent_id = agent_resp.json()["id"]
    existing = await client.post(
        "/api/v1/customers", headers=_auth(tokens), json={"name": "Repeat", "phone": "+15559990002"}
    )
    assert existing.status_code == 201, existing.text

    resp = await client.post(
        "/api/v1/calls",
        headers=_auth(tokens),
        json={
            "direction": "outbound",
            "to_number": "+15559990002",
            "from_number": "+15550001234",
            "ai_agent_id": agent_id,
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["customer_id"] == existing.json()["id"]

    # No CUSTOMER_CREATED for a number that already had a Customer row —
    # the customers.py POST above is the only creation event.
    assert calls == []


async def test_inbound_call_to_new_number_creates_customer_and_fires_trigger(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _capture(monkeypatch, "app.api.v1.telephony.enqueue_workflow_trigger")
    tokens = await _signup(client, slug="inbound-co", email="owner@inboundco.com")

    agent_resp = await client.post(
        "/api/v1/ai-agents", headers=_auth(tokens), json={"name": "Receptionist"}
    )
    agent_id = agent_resp.json()["id"]
    await client.post(
        "/api/v1/phone-numbers",
        headers=_auth(tokens),
        json={"e164_number": "+15559998888", "ai_agent_id": agent_id},
    )

    webhook_resp = await client.post(
        "/api/v1/webhooks/telephony/twilio/voice",
        data={"To": "+15559998888", "From": "+15551112222", "CallSid": "CAinboundfake"},
    )
    assert webhook_resp.status_code == 200, webhook_resp.text

    assert len(calls) == 1
    assert calls[0]["trigger"] == WorkflowTrigger.CUSTOMER_CREATED
    assert calls[0]["payload"]["phone"] == "+15551112222"

    customer = (
        await db_session.execute(select(Customer).where(Customer.phone == "+15551112222"))
    ).scalar_one()
    assert calls[0]["payload"]["customer_id"] == str(customer.id)


def _final_completion(content: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


def _sign(body: bytes) -> str:
    digest = hmac.new(b"test-meta-app-secret", body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


async def test_whatsapp_inbound_message_fires_customer_created_and_message_received(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _capture(monkeypatch, "app.services.channels.enqueue_workflow_trigger")
    tokens = await _signup(client, slug="wa-trigger-co", email="owner@watriggerco.com")

    agent_resp = await client.post(
        "/api/v1/ai-agents", headers=_auth(tokens), json={"name": "Support"}
    )
    agent_id = agent_resp.json()["id"]

    account_resp = await client.post(
        "/api/v1/whatsapp-accounts",
        headers=_auth(tokens),
        json={
            "phone_number_id": "999888777",
            "waba_id": "111222333",
            "display_phone_number": "+15550001111",
            "access_token": "fake-token",
        },
    )
    assert account_resp.status_code == 201, account_resp.text

    from packages.database.models import WhatsAppAccount

    account = await db_session.get(WhatsAppAccount, uuid.UUID(account_resp.json()["id"]))
    account.ai_agent_id = uuid.UUID(agent_id)
    await db_session.commit()

    async def fake_chat_completion(*, messages, tools=None, api_key=None, **kwargs):
        return _final_completion("Sure, how can I help?")

    async def fake_send_text_message(account, *, to, body, metered=True):
        return {"messages": [{"id": "wamid.fake"}]}

    monkeypatch.setattr("app.services.agent_engine.chat_completion", fake_chat_completion)
    monkeypatch.setattr("app.services.channels.send_text_message", fake_send_text_message)

    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": "999888777"},
                            "messages": [
                                {
                                    "from": "15556660000",
                                    "type": "text",
                                    "text": {"body": "What are your hours?"},
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

    triggers_fired = [c["trigger"] for c in calls]
    assert WorkflowTrigger.CUSTOMER_CREATED in triggers_fired
    assert WorkflowTrigger.MESSAGE_RECEIVED in triggers_fired

    message_event = next(c for c in calls if c["trigger"] == WorkflowTrigger.MESSAGE_RECEIVED)
    assert message_event["payload"]["channel"] == "whatsapp"
    assert message_event["payload"]["text"] == "What are your hours?"


async def test_voice_call_turn_fires_message_received_with_voice_channel(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _capture(monkeypatch, "app.api.v1.internal.enqueue_workflow_trigger")
    tokens = await _signup(client, slug="voice-trigger-co", email="owner@voicetriggerco.com")

    agent_resp = await client.post(
        "/api/v1/ai-agents", headers=_auth(tokens), json={"name": "Receptionist"}
    )
    agent_id = agent_resp.json()["id"]

    tenant = (
        await db_session.execute(select(Tenant).where(Tenant.slug == "voice-trigger-co"))
    ).scalar_one()
    customer = Customer(tenant_id=tenant.id, name="Caller", phone="+15550009999")
    db_session.add(customer)
    await db_session.flush()
    call = Call(
        tenant_id=tenant.id,
        customer_id=customer.id,
        ai_agent_id=uuid.UUID(agent_id),
        direction=CallDirection.INBOUND,
        status=CallStatus.IN_PROGRESS,
    )
    db_session.add(call)
    await db_session.commit()

    async def fake_chat_completion(*, messages, tools=None, api_key=None, **kwargs):
        return _final_completion("Sure, I can help with that.")

    monkeypatch.setattr("app.services.agent_engine.chat_completion", fake_chat_completion)

    resp = await client.post(
        f"/api/v1/internal/calls/{call.id}/turn",
        headers=_internal_headers(),
        json={"transcript": "I need to reschedule my appointment"},
    )
    assert resp.status_code == 200, resp.text

    assert len(calls) == 1
    assert calls[0]["trigger"] == WorkflowTrigger.MESSAGE_RECEIVED
    assert calls[0]["payload"]["channel"] == "voice"
    assert calls[0]["payload"]["customer_id"] == str(customer.id)
    assert calls[0]["payload"]["text"] == "I need to reschedule my appointment"


async def test_create_ticket_tool_does_not_recursively_refire_ticket_created(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression guard: a tenant could configure trigger=TICKET_CREATED,
    action=create_ticket. If the tool dispatched TICKET_CREATED itself,
    that workflow would recreate a ticket forever. It must not."""
    from app.workflows.dispatch import run_pending_workflows

    calls = _capture(monkeypatch, "app.workflows.dispatch.enqueue_workflow_trigger")

    tenant = Tenant(name="Loop Co", slug=f"loop-co-{uuid.uuid4().hex[:8]}")
    db_session.add(tenant)
    await db_session.flush()
    customer = Customer(tenant_id=tenant.id, name="Loopy", phone="+15551119999")
    db_session.add(customer)
    await db_session.flush()

    # Trigger=CALL_ENDED so this test's own call doesn't recurse into itself;
    # a second workflow on TICKET_CREATED proves it's simply never invoked.
    db_session.add(
        Workflow(
            tenant_id=tenant.id,
            name="Ticket on call end",
            trigger=WorkflowTrigger.CALL_ENDED,
            is_active=True,
            definition={
                "nodes": [
                    {
                        "key": "n1",
                        "type": "action",
                        "action": "create_ticket",
                        "params": {"subject": "Follow up", "customer_id": "{{customer_id}}"},
                    }
                ]
            },
        )
    )
    db_session.add(
        Workflow(
            tenant_id=tenant.id,
            name="Runaway loop if ever triggered",
            trigger=WorkflowTrigger.TICKET_CREATED,
            is_active=True,
            definition={
                "nodes": [
                    {
                        "key": "n1",
                        "type": "action",
                        "action": "create_ticket",
                        "params": {"subject": "Loop", "customer_id": "{{customer_id}}"},
                    }
                ]
            },
        )
    )
    await db_session.commit()

    await run_pending_workflows(
        db_session,
        tenant_id=tenant.id,
        trigger=WorkflowTrigger.CALL_ENDED,
        payload={"customer_id": str(customer.id), "status": "completed"},
    )

    # The create_ticket action ran (proving the workflow itself executed),
    # but it must not have dispatched TICKET_CREATED — so `calls` is empty
    # and only one Ticket exists, not an unbounded chain of them.
    tickets = (
        await db_session.execute(select(Ticket).where(Ticket.tenant_id == tenant.id))
    ).scalars().all()
    assert len(tickets) == 1
    assert calls == []
