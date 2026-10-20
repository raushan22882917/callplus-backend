"""The agent knows a customer's bookings and recent messages on every channel."""

import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.agent_engine import _enabled_tools_for_agent
from app.services.customer_context import bookings_note, customer_bookings, other_channel_note, spoken_time
from app.tools.context import ToolContext
from app.tools.registry import execute_tool
from packages.database.models import (
    AIAgent,
    AIAgentTool,
    Conversation,
    ConversationChannel,
    Customer,
    Message,
    MessageDirection,
    MessageSender,
    Tenant,
)
from packages.database.models.operations import Appointment, AppointmentResource, BookableService

HOURS = {d: [["09:00", "20:00"]] for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}


async def _salon(db: AsyncSession) -> dict:
    tenant = Tenant(name="Salon", slug=f"salon-{uuid.uuid4().hex[:8]}")
    db.add(tenant)
    await db.flush()
    customer = Customer(tenant_id=tenant.id, name="Roshan", phone="+919699234605")
    service = BookableService(tenant_id=tenant.id, name="Haircut", duration_minutes=30, price=300)
    resource = AppointmentResource(tenant_id=tenant.id, name="Priya", working_hours=HOURS, timezone="Asia/Kolkata")
    agent = AIAgent(tenant_id=tenant.id, name="Desk", system_prompt="You are Sam.")
    db.add_all([customer, service, resource, agent])
    await db.flush()
    start = datetime.now(UTC) + timedelta(days=1)
    upcoming = Appointment(
        tenant_id=tenant.id, customer_id=customer.id, service_id=service.id, resource_id=resource.id,
        start_at=start, end_at=start + timedelta(minutes=30), status="booked",
    )
    cancelled = Appointment(
        tenant_id=tenant.id, customer_id=customer.id, service_id=service.id, resource_id=resource.id,
        start_at=start + timedelta(days=2), end_at=start + timedelta(days=2, minutes=30), status="cancelled",
    )
    db.add_all([upcoming, cancelled])
    await db.commit()
    return {"tenant": tenant, "customer": customer, "agent": agent, "upcoming": upcoming, "start": start}


async def test_upcoming_bookings_are_in_the_agents_instructions(db_session: AsyncSession) -> None:
    s = await _salon(db_session)
    bookings = await customer_bookings(db_session, s["tenant"].id, s["customer"].id)
    note = bookings_note(bookings)
    assert f"Haircut with Priya, {spoken_time(s['start'], 'Asia/Kolkata')}" in note
    assert str(s["upcoming"].id) in note
    assert note.count("- Haircut") == 1  # the cancelled one is not offered as upcoming
    assert "no upcoming bookings" in bookings_note([])


async def test_my_bookings_tool_and_booking_tools_switch_on_with_services(db_session: AsyncSession) -> None:
    s = await _salon(db_session)
    ctx = ToolContext(
        db=db_session, tenant_id=s["tenant"].id, agent_id=s["agent"].id, call_id=None,
        conversation_id=None, customer_id=s["customer"].id,
    )
    out = await execute_tool("list_my_appointments", ctx, {"include_past": True})
    assert out["count"] == 2 and {a["status"] for a in out["appointments"]} == {"booked", "cancelled"}

    names = {t["function"]["name"] for t in await _enabled_tools_for_agent(db_session, s["agent"].id)}
    assert {"check_availability", "book_appointment", "list_my_appointments"} <= names

    # An agent whose tools were chosen by hand keeps that choice, plus the read-only lookup.
    db_session.add(AIAgentTool(tenant_id=s["tenant"].id, agent_id=s["agent"].id, tool_name="create_ticket", enabled=True))
    await db_session.commit()
    names = {t["function"]["name"] for t in await _enabled_tools_for_agent(db_session, s["agent"].id)}
    assert "book_appointment" not in names and {"create_ticket", "list_my_appointments"} <= names


async def test_a_call_knows_what_the_customer_said_on_whatsapp(db_session: AsyncSession) -> None:
    s = await _salon(db_session)
    tenant_id, customer_id = s["tenant"].id, s["customer"].id
    chat = Conversation(tenant_id=tenant_id, customer_id=customer_id, channel=ConversationChannel.WHATSAPP)
    call = Conversation(tenant_id=tenant_id, customer_id=customer_id, channel=ConversationChannel.VOICE)
    db_session.add_all([chat, call])
    await db_session.flush()
    db_session.add(
        Message(
            tenant_id=tenant_id, conversation_id=chat.id, direction=MessageDirection.INBOUND,
            sender=MessageSender.CUSTOMER, text="How much is hair colour?", created_at=datetime.now(UTC),
        )
    )
    await db_session.commit()
    note = await other_channel_note(db_session, call)
    assert "on WhatsApp: Customer: How much is hair colour?" in note
    assert await other_channel_note(db_session, chat) == ""  # nothing on other channels for the chat itself


async def test_whatsapp_question_about_a_booking_is_answered_from_the_calendar(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    signup = await client.post(
        "/api/v1/auth/signup",
        json={"company_name": "Cut", "tenant_slug": "cut-co", "full_name": "O", "email": "o@cut.co", "password": "supersecret1"},
    )
    auth = {"Authorization": f"Bearer {signup.json()['access_token']}"}
    agent = (await client.post("/api/v1/ai-agents", headers=auth, json={"name": "Desk"})).json()
    await client.post(
        "/api/v1/whatsapp-accounts", headers=auth,
        json={"phone_number_id": "777", "waba_id": "w", "display_phone_number": "+1555", "access_token": "t"},
    )
    tenant_id = (await db_session.get(AIAgent, uuid.UUID(agent["id"]))).tenant_id
    customer = Customer(tenant_id=tenant_id, name="Roshan", phone="+919699234605")
    service = BookableService(tenant_id=tenant_id, name="Haircut", duration_minutes=30, price=300)
    resource = AppointmentResource(tenant_id=tenant_id, name="Priya", working_hours=HOURS, timezone="Asia/Kolkata")
    db_session.add_all([customer, service, resource])
    await db_session.flush()
    start = datetime.now(UTC) + timedelta(days=1)
    db_session.add(
        Appointment(
            tenant_id=tenant_id, customer_id=customer.id, service_id=service.id, resource_id=resource.id,
            start_at=start, end_at=start + timedelta(minutes=30), status="booked",
        )
    )
    await db_session.commit()

    prompts: list = []

    async def fake_llm(*, messages, tools=None, api_key=None, **kwargs):
        prompts.append((messages[0]["content"], {t["function"]["name"] for t in tools or []}))
        return {"choices": [{"message": {"role": "assistant", "content": "Yes, tomorrow with Priya."}}]}

    async def fake_send(account, *, to, body, metered=True):
        return {"messages": [{"id": "out"}]}

    async def noop(*args, **kwargs):
        return None

    async def first_time(message_id):
        return True

    monkeypatch.setattr("app.services.agent_engine.chat_completion", fake_llm)
    monkeypatch.setattr("app.services.channels.send_text_message", fake_send)
    monkeypatch.setattr("app.services.whatsapp_inbound.mark_read", noop)
    monkeypatch.setattr("app.services.whatsapp_inbound.claim", first_time)

    body = json.dumps({"entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id": "777"},
        "messages": [{"id": "q1", "from": "919699234605", "type": "text", "text": {"body": "Do I have any booking?"}}],
    }}]}]}).encode()
    resp = await client.post(
        "/api/v1/webhooks/whatsapp/meta", content=body,
        headers={"X-Hub-Signature-256": "sha256=" + hmac.new(b"test-meta-app-secret", body, hashlib.sha256).hexdigest(), "Content-Type": "application/json"},
    )
    assert resp.status_code == 200
    system_prompt, tools = prompts[0]
    assert "Haircut with Priya" in system_prompt and "Today is" in system_prompt
    assert "book_appointment" in tools and "list_my_appointments" in tools
