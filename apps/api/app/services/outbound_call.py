"""Places an outbound AI phone call for a tenant, driven by an MCP tool call
(app/mcp/server.py:trigger_call) rather than a human, JWT-authenticated
request — the same underlying steps as POST /api/v1/calls with
direction=outbound (app/api/v1/calls.py), but callable without a
RequestContext, and able to fall back to a tenant's only phone number/agent
when the caller doesn't name one.
"""

import uuid

from app.core.config import get_settings
from app.integrations.credentials import get_twilio_credentials
from app.integrations.twilio_client import TwilioClientError, place_outbound_call
from app.services.cost_controls import SpendGuardError, guard_outbound
from app.services.customer_lookup import find_or_create_customer_by_phone
from app.services.telephony_plus import check_outbound_allowed
from app.services.voicemail_detection import amd_callback_for_agent
from app.workflows.dispatch import enqueue_workflow_trigger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from twilio.base.exceptions import TwilioException

from packages.database.models import (
    AgentStatus,
    AIAgent,
    Call,
    CallDirection,
    CallStatus,
    PhoneNumber,
    WorkflowTrigger,
)


class OutboundCallError(Exception):
    """A user-facing reason the call couldn't be placed — safe to return
    verbatim to the MCP client/LLM, unlike an arbitrary exception."""


async def _resolve_agent_and_number(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    ai_agent_id: str | None,
    from_number: str | None,
) -> tuple[uuid.UUID, str]:
    resolved_agent_id: uuid.UUID | None
    try:
        resolved_agent_id = uuid.UUID(ai_agent_id) if ai_agent_id else None
    except ValueError as exc:
        raise OutboundCallError(f"ai_agent_id is not a valid id: {ai_agent_id!r}") from exc
    resolved_from_number = from_number

    if resolved_from_number is None:
        query = select(PhoneNumber).where(PhoneNumber.tenant_id == tenant_id)
        if resolved_agent_id is not None:
            query = query.where(PhoneNumber.ai_agent_id == resolved_agent_id)
        numbers = (await db.execute(query)).scalars().all()
        if len(numbers) != 1:
            raise OutboundCallError(
                "from_number is required: this tenant has "
                f"{len(numbers)} phone number(s)"
                + (" assigned to that agent" if resolved_agent_id else "")
                + ", not exactly one"
            )
        resolved_from_number = numbers[0].e164_number
        if resolved_agent_id is None:
            resolved_agent_id = numbers[0].ai_agent_id

    if resolved_agent_id is None:
        agents = (
            (
                await db.execute(
                    select(AIAgent).where(
                        AIAgent.tenant_id == tenant_id, AIAgent.status == AgentStatus.ACTIVE
                    )
                )
            )
            .scalars()
            .all()
        )
        if len(agents) != 1:
            raise OutboundCallError(
                "ai_agent_id is required: this tenant has "
                f"{len(agents)} active AI agent(s), not exactly one"
            )
        resolved_agent_id = agents[0].id
    else:
        agent = await db.get(AIAgent, resolved_agent_id)
        if agent is None or agent.tenant_id != tenant_id:
            raise OutboundCallError(f"no AI agent {resolved_agent_id} for this tenant")

    return resolved_agent_id, resolved_from_number


async def place_ai_outbound_call(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    to_number: str,
    ai_agent_id: str | None = None,
    from_number: str | None = None,
) -> Call:
    """Commits and returns the created Call row. Raises OutboundCallError
    for anything the caller can fix (ambiguous agent/number, Twilio
    rejecting the call) — never raised for a platform bug, which surfaces
    as an ordinary unhandled exception instead."""
    resolved_agent_id, resolved_from_number = await _resolve_agent_and_number(
        db, tenant_id, ai_agent_id=ai_agent_id, from_number=from_number
    )

    decision = await check_outbound_allowed(db, tenant_id, to_number)
    if not decision.allowed:
        raise OutboundCallError(
            f"{to_number} cannot be called: it is on this tenant's "
            + ("blocklist" if decision.reason == "blocked" else "do-not-disturb registry")
        )

    try:
        await guard_outbound(db, tenant_id)
    except SpendGuardError as exc:
        raise OutboundCallError(exc.message) from exc

    customer, created = await find_or_create_customer_by_phone(db, tenant_id, to_number)

    call = Call(
        tenant_id=tenant_id,
        customer_id=customer.id,
        ai_agent_id=resolved_agent_id,
        direction=CallDirection.OUTBOUND,
        to_number=to_number,
        from_number=resolved_from_number,
    )
    db.add(call)
    await db.flush()

    settings = get_settings()
    voice_webhook_url = (
        f"{settings.public_base_url}/api/v1/webhooks/telephony/twilio/voice?call_id={call.id}"
    )
    status_callback_url = f"{settings.public_base_url}/api/v1/webhooks/telephony/twilio/status"
    try:
        credentials = await get_twilio_credentials(db, tenant_id)
        call_sid = place_outbound_call(
            to=to_number,
            from_=resolved_from_number,
            voice_webhook_url=voice_webhook_url,
            status_callback_url=status_callback_url,
            credentials=credentials,
            amd_callback_url=await amd_callback_for_agent(db, resolved_agent_id),
        )
    except (TwilioClientError, TwilioException) as exc:
        raise OutboundCallError(str(exc)) from exc

    call.provider_call_sid = call_sid
    call.status = CallStatus.RINGING
    await db.commit()
    await db.refresh(call)

    if created:
        enqueue_workflow_trigger(
            tenant_id=tenant_id,
            trigger=WorkflowTrigger.CUSTOMER_CREATED,
            payload={"customer_id": str(customer.id), "phone": to_number, "source": "call"},
        )

    return call
