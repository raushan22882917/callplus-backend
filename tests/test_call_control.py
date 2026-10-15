import uuid
from datetime import UTC, datetime, timedelta

from app.core.config import get_settings
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import (
    Call,
    CallDirection,
    CallStatus,
    Conversation,
    ConversationChannel,
    Customer,
    Message,
    MessageDirection,
    MessageSender,
    Tenant,
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


async def test_agent_call_control_fields_are_configurable(client: AsyncClient) -> None:
    tokens = await _signup(client, slug="control-co", email="owner@controlco.com")
    resp = await client.post(
        "/api/v1/ai-agents",
        headers=_auth(tokens),
        json={
            "name": "Receptionist",
            "max_call_duration_seconds": 60,
            "max_no_response_retries": 1,
            "no_response_timeout_seconds": 4,
            "no_response_prompt": "Are you still there?",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["max_call_duration_seconds"] == 60
    assert body["max_no_response_retries"] == 1
    assert body["no_response_timeout_seconds"] == 4
    assert body["no_response_prompt"] == "Are you still there?"


async def _make_in_progress_call(
    db: AsyncSession, *, tenant_slug: str, agent_id: str, started_seconds_ago: int
) -> Call:
    tenant = (
        await db.execute(select(Tenant).where(Tenant.slug == tenant_slug))
    ).scalar_one()
    customer = Customer(tenant_id=tenant.id, name="Jane", phone="+15550001111")
    db.add(customer)
    await db.flush()
    call = Call(
        tenant_id=tenant.id,
        customer_id=customer.id,
        ai_agent_id=uuid.UUID(agent_id),
        direction=CallDirection.INBOUND,
        status=CallStatus.IN_PROGRESS,
        started_at=datetime.now(UTC) - timedelta(seconds=started_seconds_ago),
    )
    db.add(call)
    await db.commit()
    return call


async def test_call_turn_hangs_up_when_duration_exceeded(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    tokens = await _signup(client, slug="duration-co", email="owner@durationco.com")
    agent_resp = await client.post(
        "/api/v1/ai-agents",
        headers=_auth(tokens),
        json={
            "name": "Receptionist",
            "max_call_duration_seconds": 30,
            "max_duration_message": "Time is up, goodbye.",
        },
    )
    agent_id = agent_resp.json()["id"]

    call = await _make_in_progress_call(
        db_session, tenant_slug="duration-co", agent_id=agent_id, started_seconds_ago=45
    )

    turn_resp = await client.post(
        f"/api/v1/internal/calls/{call.id}/turn",
        headers=_internal_headers(),
        json={"transcript": "hello"},
    )
    assert turn_resp.status_code == 200, turn_resp.text
    body = turn_resp.json()
    assert body["action"] == "hangup"
    assert body["reply_text"] == "Time is up, goodbye."
    assert body["reason"] == "max_duration_exceeded"


async def test_call_finalize_hangup_is_idempotent(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    tokens = await _signup(client, slug="finalize-co", email="owner@finalizeco.com")
    agent_resp = await client.post(
        "/api/v1/ai-agents", headers=_auth(tokens), json={"name": "Receptionist"}
    )
    agent_id = agent_resp.json()["id"]
    call = await _make_in_progress_call(
        db_session, tenant_slug="finalize-co", agent_id=agent_id, started_seconds_ago=5
    )

    resp1 = await client.post(
        f"/api/v1/internal/calls/{call.id}/finalize",
        headers=_internal_headers(),
        json={"action": "hangup", "reason": "no_response_exhausted"},
    )
    assert resp1.status_code == 200, resp1.text
    assert resp1.json()["status"] == "completed"

    resp2 = await client.post(
        f"/api/v1/internal/calls/{call.id}/finalize",
        headers=_internal_headers(),
        json={"action": "hangup", "reason": "no_response_exhausted"},
    )
    assert resp2.status_code == 200
    assert resp2.json()["status"] == "completed"


async def test_call_transcript_returns_messages_in_order(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    tokens = await _signup(client, slug="transcript-co", email="owner@transcriptco.com")
    agent_resp = await client.post(
        "/api/v1/ai-agents", headers=_auth(tokens), json={"name": "Receptionist"}
    )
    agent_id = agent_resp.json()["id"]
    call = await _make_in_progress_call(
        db_session, tenant_slug="transcript-co", agent_id=agent_id, started_seconds_ago=30
    )

    conversation = Conversation(
        tenant_id=call.tenant_id,
        customer_id=call.customer_id,
        channel=ConversationChannel.VOICE,
        call_id=call.id,
    )
    db_session.add(conversation)
    await db_session.flush()
    db_session.add_all(
        [
            Message(
                tenant_id=call.tenant_id,
                conversation_id=conversation.id,
                direction=MessageDirection.INBOUND,
                sender=MessageSender.CUSTOMER,
                text="I want to book a haircut",
            ),
            Message(
                tenant_id=call.tenant_id,
                conversation_id=conversation.id,
                direction=MessageDirection.OUTBOUND,
                sender=MessageSender.AI_AGENT,
                text="Sure, what time works for you?",
            ),
        ]
    )
    await db_session.commit()

    resp = await client.get(f"/api/v1/calls/{call.id}/transcript", headers=_auth(tokens))
    assert resp.status_code == 200, resp.text
    messages = resp.json()
    assert [m["text"] for m in messages] == [
        "I want to book a haircut",
        "Sure, what time works for you?",
    ]
    assert [m["sender"] for m in messages] == ["customer", "ai_agent"]


async def test_call_transcript_is_empty_for_call_with_no_conversation(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    tokens = await _signup(client, slug="no-transcript-co", email="owner@notranscriptco.com")
    agent_resp = await client.post(
        "/api/v1/ai-agents", headers=_auth(tokens), json={"name": "Receptionist"}
    )
    agent_id = agent_resp.json()["id"]
    call = await _make_in_progress_call(
        db_session, tenant_slug="no-transcript-co", agent_id=agent_id, started_seconds_ago=5
    )

    resp = await client.get(f"/api/v1/calls/{call.id}/transcript", headers=_auth(tokens))
    assert resp.status_code == 200, resp.text
    assert resp.json() == []
