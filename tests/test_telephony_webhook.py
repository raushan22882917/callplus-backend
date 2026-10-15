import uuid

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import Call, CallDirection, CallStatus, Tenant


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


async def test_inbound_call_to_known_number_creates_call_and_returns_stream_twiml(
    client: AsyncClient,
) -> None:
    tokens = await _signup(client, slug="vet-clinic", email="owner@vetclinic.com")

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
        data={"To": "+15559998888", "From": "+15551112222", "CallSid": "CAxxxxTESTxxxx"},
    )
    assert webhook_resp.status_code == 200
    assert webhook_resp.headers["content-type"].startswith("application/xml")
    assert "<Stream" in webhook_resp.text
    assert "callId" in webhook_resp.text

    calls_resp = await client.get("/api/v1/calls", headers=_auth(tokens))
    calls = calls_resp.json()
    assert len(calls) == 1
    assert calls[0]["from_number"] == "+15551112222"
    assert calls[0]["ai_agent_id"] == agent_id
    assert calls[0]["status"] == "ringing"


async def test_inbound_call_to_unknown_number_is_rejected(client: AsyncClient) -> None:
    webhook_resp = await client.post(
        "/api/v1/webhooks/telephony/twilio/voice",
        data={"To": "+15550000000", "From": "+15551112222", "CallSid": "CAxxxxUNKNOWNxxxx"},
    )
    assert webhook_resp.status_code == 200
    assert "<Reject" in webhook_resp.text


async def test_outbound_call_answer_webhook_sets_started_at(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    tokens = await _signup(client, slug="outbound-clinic", email="owner@outboundclinic.com")

    agent_resp = await client.post(
        "/api/v1/ai-agents", headers=_auth(tokens), json={"name": "Receptionist"}
    )
    agent_id = agent_resp.json()["id"]

    tenant_result = await db_session.execute(
        select(Tenant).where(Tenant.slug == "outbound-clinic")
    )
    tenant = tenant_result.scalar_one()

    call = Call(
        tenant_id=tenant.id,
        ai_agent_id=uuid.UUID(agent_id),
        provider_call_sid="CAxxxxOUTBOUNDxxxx",
        direction=CallDirection.OUTBOUND,
        status=CallStatus.RINGING,
        from_number="+15559998888",
        to_number="+15551112222",
        started_at=None,
    )
    db_session.add(call)
    await db_session.commit()
    await db_session.refresh(call)
    assert call.started_at is None

    webhook_resp = await client.post(
        f"/api/v1/webhooks/telephony/twilio/voice?call_id={call.id}",
        data={"To": "+15551112222", "From": "+15559998888", "CallSid": "CAxxxxOUTBOUNDxxxx"},
    )
    assert webhook_resp.status_code == 200
    assert "<Stream" in webhook_resp.text

    await db_session.refresh(call)
    assert call.started_at is not None
