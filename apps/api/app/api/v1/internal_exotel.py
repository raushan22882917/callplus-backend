"""Gateway-facing endpoint for Exotel calls (see app/services/exotel.py).

When Exotel connects a call to the realtime gateway, the gateway asks here who owns it. This does
what the Twilio voice webhook does for an inbound call (same blocklist, spam, plan and wallet
checks, same customer and call records) and answers with the call id the gateway then uses for
every turn. A refusal is a 4xx and the gateway hangs up.
"""

import logging
import uuid
from datetime import UTC, datetime, timedelta

from app.api.v1.telephony import _check_and_apply_spam_protection, _is_blocked
from app.core.deps import get_db
from app.core.internal_auth import require_internal_service
from app.services import exotel, plans
from app.services.call_finalize import TERMINAL_CALL_STATUSES
from app.services.customer_lookup import find_or_create_customer_by_phone
from app.workflows.dispatch import enqueue_workflow_trigger
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import (
    Call,
    CallDirection,
    CallStatus,
    PhoneNumber,
    Tenant,
    WorkflowTrigger,
)

logger = logging.getLogger(__name__)
OUTBOUND_MATCH_WINDOW = timedelta(minutes=5)

router = APIRouter(
    prefix="/internal/exotel", tags=["internal"], dependencies=[Depends(require_internal_service)]
)


class ExotelStart(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    stream_key: str = Field(min_length=8, max_length=200)
    call_sid: str = Field(min_length=1, max_length=100)
    from_number: str = Field(default="", alias="from", max_length=40)
    to: str = Field(default="", max_length=40)


class ExotelStartResponse(BaseModel):
    call_id: uuid.UUID
    tenant_id: uuid.UUID


async def _outbound_call(
    db: AsyncSession, tenant_id: uuid.UUID, call_sid: str, from_number: str, to_number: str
) -> Call | None:
    """The outbound call this stream belongs to, if we placed it (see exotel.place_outbound_call).

    Matched by Exotel's CallSid; failing that, by a call we are still ringing between the same two
    numbers (on an outbound call Exotel reports the customer as "from" and the ExoPhone as "to")."""
    result = await db.execute(
        select(Call).where(
            Call.tenant_id == tenant_id,
            Call.provider_call_sid == call_sid,
            Call.direction == CallDirection.OUTBOUND,
        )
    )
    call = result.scalar_one_or_none()
    if call is not None or not from_number:
        return call
    result = await db.execute(
        select(Call)
        .where(
            Call.tenant_id == tenant_id,
            Call.direction == CallDirection.OUTBOUND,
            Call.status.in_([CallStatus.QUEUED, CallStatus.RINGING]),
            Call.to_number == from_number,
            Call.from_number == to_number,
            Call.created_at > datetime.now(UTC) - OUTBOUND_MATCH_WINDOW,
        )
        .order_by(Call.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


@router.post("/start", response_model=ExotelStartResponse)
async def start_call(
    payload: ExotelStart, db: AsyncSession = Depends(get_db)
) -> ExotelStartResponse:
    tenant_id = await exotel.tenant_for_stream_key(db, payload.stream_key)
    if tenant_id is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Unknown stream address")

    to_number = exotel.to_e164(payload.to)
    from_number = exotel.to_e164(payload.from_number)
    outbound = await _outbound_call(db, tenant_id, payload.call_sid, from_number, to_number)
    if outbound is not None:
        # One of our own outbound calls was answered: its checks ran when it was placed.
        if outbound.status not in (CallStatus.IN_PROGRESS, *TERMINAL_CALL_STATUSES):
            outbound.status = CallStatus.IN_PROGRESS
            outbound.started_at = outbound.started_at or datetime.now(UTC)
            await db.commit()
        return ExotelStartResponse(call_id=outbound.id, tenant_id=tenant_id)

    result = await db.execute(
        select(PhoneNumber).where(
            PhoneNumber.tenant_id == tenant_id,
            PhoneNumber.e164_number == to_number,
            PhoneNumber.provider == "exotel",
        )
    )
    phone_number = result.scalar_one_or_none()
    if phone_number is None or phone_number.ai_agent_id is None:
        # Not one of this workspace's Exotel numbers, or nothing is set to answer it.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No AI agent answers this number")
    if await _is_blocked(db, tenant_id, from_number):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Caller is blocked")
    tenant = await db.get(Tenant, tenant_id)
    if await _check_and_apply_spam_protection(db, tenant, from_number):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Caller is blocked")
    try:
        await plans.ensure_inbound_allowed(db, tenant_id)
    except plans.PlanLimitError as exc:
        logger.warning("exotel call refused for tenant %s: %s", tenant_id, exc.code)
        raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, exc.message) from exc

    # A retry of the same call (the gateway reconnected) must not create a second call.
    existing = await db.execute(
        select(Call).where(Call.tenant_id == tenant_id, Call.provider_call_sid == payload.call_sid)
    )
    call = existing.scalar_one_or_none()
    if call is None:
        customer, created = await find_or_create_customer_by_phone(db, tenant_id, from_number)
        call = Call(
            tenant_id=tenant_id,
            customer_id=customer.id,
            ai_agent_id=phone_number.ai_agent_id,
            provider_call_sid=payload.call_sid,
            direction=CallDirection.INBOUND,
            status=CallStatus.IN_PROGRESS,
            from_number=from_number,
            to_number=to_number,
            started_at=datetime.now(UTC),
        )
        db.add(call)
        await db.flush()
        await db.commit()
        if created:
            enqueue_workflow_trigger(
                tenant_id=tenant_id,
                trigger=WorkflowTrigger.CUSTOMER_CREATED,
                payload={
                    "customer_id": str(customer.id),
                    "phone": customer.phone,
                    "source": "call",
                },
            )
    return ExotelStartResponse(call_id=call.id, tenant_id=tenant_id)
