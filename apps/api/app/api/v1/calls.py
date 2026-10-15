import uuid
from datetime import UTC, datetime
from urllib.parse import urlparse

import httpx
from app.core.audit import log_audit_event
from app.core.config import get_settings
from app.core.deps import RequestContext, get_db, require_permission
from app.core.permissions import Permissions
from app.core.repository import TenantScopedRepository
from app.integrations.credentials import get_twilio_credentials
from app.integrations.twilio_client import TwilioClientError, place_outbound_call
from app.schemas.call import CallCreate, CallRead
from app.schemas.conversation import MessageRead
from app.services.call_analysis import analyze_call
from app.services.call_finalize import emit_call_webhooks
from app.services.customer_lookup import find_or_create_customer_by_phone
from app.services.voicemail_detection import amd_callback_for_agent
from app.services.platform_masking import masked_calls, masked_messages
from app.services.webhooks import enqueue_webhook_event
from app.workflows.dispatch import enqueue_workflow_trigger
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from twilio.base.exceptions import TwilioException

from packages.database.models import (
    AIAgent,
    Call,
    CallDirection,
    CallStatus,
    Conversation,
    IdempotencyRecord,
    Message,
    WorkflowTrigger,
)

router = APIRouter(prefix="/calls", tags=["calls"])


class CallRepository(TenantScopedRepository[Call]):
    model = Call


@router.get("", response_model=list[CallRead])
async def list_calls(
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    ai_agent_id: uuid.UUID | None = Query(default=None),
    direction: CallDirection | None = Query(default=None),
    call_status: CallStatus | None = Query(default=None, alias="status"),
    sentiment: str | None = Query(default=None),
    outcome: str | None = Query(default=None),
    q: str | None = Query(default=None, max_length=100, description="Search summary, intent and caller number"),
    ctx: RequestContext = Depends(require_permission(Permissions.CALLS_READ)),
    db: AsyncSession = Depends(get_db),
) -> list[CallRead]:
    query = select(Call).where(Call.tenant_id == ctx.tenant_id)
    if ai_agent_id is not None:
        query = query.where(Call.ai_agent_id == ai_agent_id)
    if direction is not None:
        query = query.where(Call.direction == direction)
    if call_status is not None:
        query = query.where(Call.status == call_status)
    if sentiment is not None:
        query = query.where(Call.sentiment == sentiment)
    if outcome is not None:
        query = query.where(Call.outcome == outcome)
    if q:
        like = f"%{q.strip()}%"
        query = query.where(
            or_(
                Call.summary.ilike(like),
                Call.intent.ilike(like),
                Call.from_number.ilike(like),
                Call.to_number.ilike(like),
            )
        )
    result = await db.execute(query.order_by(Call.created_at.desc()).limit(limit).offset(offset))
    return await masked_calls(db, ctx, result.scalars().all())


IN_FLIGHT_STATUSES = (CallStatus.QUEUED, CallStatus.RINGING, CallStatus.IN_PROGRESS)


@router.post("", response_model=CallRead, status_code=status.HTTP_201_CREATED)
async def create_call(
    payload: CallCreate,
    idempotency_key: str | None = Header(default=None, max_length=128),
    ctx: RequestContext = Depends(require_permission(Permissions.CALLS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> Call:
    if payload.direction == CallDirection.OUTBOUND:
        if not payload.to_number or not payload.from_number or not payload.ai_agent_id:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "to_number, from_number, and ai_agent_id are required for outbound calls",
            )

    if payload.ai_agent_id is not None:
        # The agent must belong to THIS tenant — the FK alone would let a caller
        # run another business's agent (and prompt) on their own call.
        agent = await db.get(AIAgent, payload.ai_agent_id)
        if agent is None or agent.tenant_id != ctx.tenant_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "AI agent not found")

    # A retried request (network blip, client timeout) must not dial twice.
    if idempotency_key:
        existing = await _call_for_idempotency_key(db, ctx.tenant_id, idempotency_key)
        if existing is not None:
            return existing

    in_flight = (
        await db.execute(
            select(func.count(Call.id)).where(
                Call.tenant_id == ctx.tenant_id, Call.status.in_(IN_FLIGHT_STATUSES)
            )
        )
    ).scalar_one()
    max_concurrent = get_settings().max_concurrent_calls_per_tenant
    if payload.direction == CallDirection.OUTBOUND and in_flight >= max_concurrent:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"Concurrent call limit reached ({max_concurrent}). Wait for a call to finish.",
            headers={"Retry-After": "10"},
        )

    is_test_call = ctx.api_key_mode == "test"
    call_data = payload.model_dump()
    new_customer_id = None
    # An outbound call to a number with no existing Customer still belongs
    # in the CRM — mirrors what inbound calls and WhatsApp already do (see
    # app/services/customer_lookup.py) instead of leaving the call orphaned.
    # Test-mode calls stay out of the CRM.
    if call_data.get("customer_id") is None and payload.to_number and not is_test_call:
        customer, created = await find_or_create_customer_by_phone(
            db, ctx.tenant_id, payload.to_number
        )
        call_data["customer_id"] = customer.id
        if created:
            new_customer_id = customer.id

    call = await CallRepository(db, ctx.tenant_id).create(**call_data)
    await db.flush()

    if idempotency_key:
        db.add(IdempotencyRecord(tenant_id=ctx.tenant_id, key=idempotency_key, call_id=call.id))
        try:
            # Claimed BEFORE dialling: a concurrent duplicate fails here and
            # returns the first request's call instead of placing a second.
            await db.flush()
        except IntegrityError:
            await db.rollback()
            existing = await _call_for_idempotency_key(db, ctx.tenant_id, idempotency_key)
            if existing is not None:
                return existing
            raise

    if is_test_call:
        # ak_test_ keys never reach Twilio, so they can't ring a phone or cost
        # money. The call still runs a full lifecycle (both directions) and
        # fires the same webhooks as a real one, so integrations can be built
        # and verified without dialling. Tenant workflows are NOT triggered.
        now = datetime.now(UTC)
        call.status = CallStatus.COMPLETED
        call.started_at = call.ended_at = now
        call.duration_seconds = 0
    elif payload.direction == CallDirection.OUTBOUND:
        settings = get_settings()
        voice_webhook_url = (
            f"{settings.public_base_url}/api/v1/webhooks/telephony/twilio/voice?call_id={call.id}"
        )
        status_callback_url = f"{settings.public_base_url}/api/v1/webhooks/telephony/twilio/status"
        try:
            credentials = await get_twilio_credentials(db, ctx.tenant_id)
            call_sid = place_outbound_call(
                to=payload.to_number,
                from_=payload.from_number,
                voice_webhook_url=voice_webhook_url,
                status_callback_url=status_callback_url,
                credentials=credentials,
                amd_callback_url=await amd_callback_for_agent(db, call.ai_agent_id),
            )
        except (TwilioClientError, TwilioException) as exc:
            await db.rollback()
            raise HTTPException(status.HTTP_424_FAILED_DEPENDENCY, str(exc)) from exc
        call.provider_call_sid = call_sid
        call.status = CallStatus.RINGING

    await db.commit()
    await db.refresh(call)

    if is_test_call:
        enqueue_webhook_event(
            tenant_id=ctx.tenant_id,
            event="call.started",
            data={
                "call_id": str(call.id),
                "customer_id": None,
                "ai_agent_id": str(call.ai_agent_id) if call.ai_agent_id else None,
                "direction": call.direction.value,
                "from_number": call.from_number,
                "to_number": call.to_number,
                "test_mode": True,
            },
        )
        await emit_call_webhooks(db, call, CallStatus.COMPLETED)

    if new_customer_id is not None:
        enqueue_workflow_trigger(
            tenant_id=ctx.tenant_id,
            trigger=WorkflowTrigger.CUSTOMER_CREATED,
            payload={
                "customer_id": str(new_customer_id),
                "phone": payload.to_number,
                "source": "call",
            },
        )

    return call


async def _call_for_idempotency_key(
    db: AsyncSession, tenant_id: uuid.UUID, key: str
) -> Call | None:
    record = (
        await db.execute(
            select(IdempotencyRecord).where(
                IdempotencyRecord.tenant_id == tenant_id, IdempotencyRecord.key == key
            )
        )
    ).scalar_one_or_none()
    if record is None:
        return None
    return await CallRepository(db, tenant_id).get(record.call_id)


@router.get("/{call_id}", response_model=CallRead)
async def get_call(
    call_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(Permissions.CALLS_READ)),
    db: AsyncSession = Depends(get_db),
) -> CallRead:
    call = await CallRepository(db, ctx.tenant_id).get(call_id)
    return (await masked_calls(db, ctx, [call]))[0]


@router.post("/{call_id}/analyze", response_model=CallRead)
async def analyze_call_now(
    call_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(Permissions.CALLS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> Call:
    """Re-runs the AI summary/sentiment/intent pass (e.g. for calls that ended before it existed)."""
    call = await CallRepository(db, ctx.tenant_id).get(call_id)
    if call.status != CallStatus.COMPLETED:
        raise HTTPException(status.HTTP_409_CONFLICT, "Only completed calls can be analysed")
    if not await analyze_call(db, call_id):
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "The AI analysis failed; try again shortly")
    await db.refresh(call)
    return call


@router.get("/{call_id}/transcript", response_model=list[MessageRead])
async def get_call_transcript(
    call_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(Permissions.CALLS_READ)),
    db: AsyncSession = Depends(get_db),
) -> list[MessageRead]:
    """The full turn-by-turn transcript for a call — every customer
    utterance and AI reply, in order. Persisted by app/services/agent_engine.py
    as the call happens, one Message row per turn on each side."""
    await CallRepository(db, ctx.tenant_id).get(call_id)  # 404s if not in this tenant

    conversation_result = await db.execute(
        select(Conversation).where(
            Conversation.tenant_id == ctx.tenant_id, Conversation.call_id == call_id
        )
    )
    conversation = conversation_result.scalar_one_or_none()
    if conversation is None:
        return []

    messages_result = await db.execute(
        select(Message)
        .where(Message.tenant_id == ctx.tenant_id, Message.conversation_id == conversation.id)
        .order_by(Message.created_at.asc())
    )
    return await masked_messages(db, ctx, messages_result.scalars().all())


@router.get("/{call_id}/recording")
async def get_call_recording(
    call_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(Permissions.CALLS_READ)),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Streams the call's recording through the API: Twilio recording URLs
    need the account's credentials, which a browser doesn't (and must not)
    have. Every access is audit-logged."""
    call = await CallRepository(db, ctx.tenant_id).get(call_id)
    if not call.recording_url:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No recording for this call")
    if urlparse(call.recording_url).hostname != "api.twilio.com":
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Unsupported recording location")

    credentials = await get_twilio_credentials(db, ctx.tenant_id)
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as http:
        upstream = await http.get(
            call.recording_url, auth=(credentials.account_sid, credentials.auth_token)
        )
    if upstream.status_code != 200:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Recording is not available yet")

    await log_audit_event(
        db,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        action="call.recording_accessed",
        resource_type="call",
        resource_id=call.id,
    )
    await db.commit()
    return Response(content=upstream.content, media_type="audio/mpeg")
