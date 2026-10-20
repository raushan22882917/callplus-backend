"""Twilio webhooks — the entry point for real phone calls. This is the one
place in the tenant-facing API where tenant_id is derived from something
other than a JWT: the phone_numbers row matched by the Twilio "To" number.
That lookup, plus Twilio's request-signature validation below, is the whole
trust boundary here — treat both as load-bearing.
"""

import uuid
from datetime import UTC, datetime, timedelta

from app.core.config import get_settings
from app.core.deps import get_db
from app.integrations.credentials import get_twilio_credentials
from app.integrations.twilio_client import build_stream_twiml, validate_twilio_signature
from app.services.call_finalize import TERMINAL_CALL_STATUSES, finalize_call
from app.services.channels_voice import (
    record_missed_call,
    voicemail_twiml_for_unassigned_number,
)
from app.services.customer_lookup import find_or_create_customer_by_phone
from app.services.telephony_plus import resolve_inbound_route, twiml_for_route
from app.services.voicemail_detection import handle_answered_by
from app.workflows.dispatch import enqueue_workflow_trigger
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import PlainTextResponse

from packages.database.models import (
    BlockedCaller,
    Call,
    CallDirection,
    CallStatus,
    Customer,
    PhoneNumber,
    Tenant,
    WorkflowTrigger,
)

router = APIRouter(prefix="/webhooks/telephony/twilio", tags=["telephony-webhooks"])

_TWILIO_STATUS_MAP = {
    "queued": CallStatus.QUEUED,
    "ringing": CallStatus.RINGING,
    "in-progress": CallStatus.IN_PROGRESS,
    "completed": CallStatus.COMPLETED,
    "busy": CallStatus.FAILED,
    "failed": CallStatus.FAILED,
    "no-answer": CallStatus.NO_ANSWER,
    "canceled": CallStatus.FAILED,
}

_REJECT_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response><Reject/></Response>'


def _reject_response() -> Response:
    return PlainTextResponse(content=_REJECT_TWIML, media_type="application/xml")


async def _validate_request(request: Request, form: dict, *, auth_token: str) -> None:
    settings = get_settings()
    if not auth_token:
        if settings.is_local:
            return  # signature validation skipped when Twilio isn't configured (local dev)
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Twilio webhook validation is not configured"
        )
    url = f"{settings.public_base_url}{request.url.path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"
    signature = request.headers.get("X-Twilio-Signature")
    is_valid = validate_twilio_signature(
        url=url, params=form, signature=signature, auth_token=auth_token
    )
    if not is_valid:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid Twilio signature")


async def _resolve_auth_token(db: AsyncSession, tenant_id: uuid.UUID | None) -> str:
    """The auth_token to verify this webhook's signature with: the owning
    tenant's own (if they've connected a BYO Twilio account) or the
    platform's. Falls back to the platform token when tenant_id is unknown
    (e.g. the "To" number doesn't map to any tenant) — still filters out
    non-Twilio-signed requests even though we can't resolve a tenant yet."""
    if tenant_id is not None:
        credentials = await get_twilio_credentials(db, tenant_id)
        return credentials.auth_token
    return get_settings().twilio_auth_token


async def _is_blocked(db: AsyncSession, tenant_id: uuid.UUID, phone: str) -> bool:
    result = await db.execute(
        select(BlockedCaller).where(
            BlockedCaller.tenant_id == tenant_id, BlockedCaller.e164_number == phone
        )
    )
    return result.scalar_one_or_none() is not None


async def _check_and_apply_spam_protection(
    db: AsyncSession, tenant: Tenant, from_number: str
) -> bool:
    """Returns True if this call should be rejected. Auto-blocks the caller
    for all future calls once they cross the tenant's own configured
    threshold — owner-configurable via PATCH /api/v1/tenants/me."""
    if not tenant.spam_protection_enabled:
        return False

    window_start = datetime.now(UTC) - timedelta(minutes=tenant.spam_call_window_minutes)
    result = await db.execute(
        select(func.count())
        .select_from(Call)
        .where(
            Call.tenant_id == tenant.id,
            Call.from_number == from_number,
            Call.created_at >= window_start,
        )
    )
    recent_call_count = result.scalar_one()
    if recent_call_count < tenant.spam_call_threshold:
        return False

    db.add(
        BlockedCaller(
            tenant_id=tenant.id,
            e164_number=from_number,
            reason=(
                f"auto: {recent_call_count} calls within {tenant.spam_call_window_minutes} minutes"
            ),
        )
    )
    await db.commit()
    return True


@router.post("/voice")
async def voice(request: Request, db: AsyncSession = Depends(get_db)) -> Response:
    form = dict((await request.form()).items())
    new_customer: Customer | None = None
    route = None  # telephony_plus: IVR / forwarding / number routing, when configured

    call_id_param = request.query_params.get("call_id")
    if call_id_param:
        # Outbound call: the Call row already exists (created when the
        # tenant API placed it — see app/api/v1/calls.py), Twilio is just
        # asking what TwiML to run now that the callee answered.
        call = await db.get(Call, uuid.UUID(call_id_param))
        if call is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Call not found")

        auth_token = await _resolve_auth_token(db, call.tenant_id)
        await _validate_request(request, form, auth_token=auth_token)

        # Only set once we know the callee actually answered (Twilio only
        # hits this webhook on answer) — without this, max_call_duration_seconds
        # can never trigger for outbound calls, since call_turn's check
        # divides by a start time that was never recorded.
        if call.started_at is None:
            call.started_at = datetime.now(UTC)
        if call.status in (CallStatus.QUEUED, CallStatus.RINGING):
            call.status = CallStatus.IN_PROGRESS
    else:
        to_number = form.get("To", "")
        from_number = form.get("From", "")
        result = await db.execute(select(PhoneNumber).where(PhoneNumber.e164_number == to_number))
        phone_number = result.scalar_one_or_none()

        auth_token = await _resolve_auth_token(db, phone_number.tenant_id if phone_number else None)
        await _validate_request(request, form, auth_token=auth_token)

        route = (
            await resolve_inbound_route(db, phone_number.tenant_id, to_number)
            if phone_number is not None
            else None
        )
        if (
            phone_number is not None
            and phone_number.ai_agent_id is None
            and route is None
            and not await _is_blocked(db, phone_number.tenant_id, from_number)
        ):
            # Owned number with nothing to answer it: take a voicemail if the
            # tenant enabled it (app/services/channels_voice.py).
            voicemail_twiml = await voicemail_twiml_for_unassigned_number(
                db, phone_number, from_number=from_number, call_sid=form.get("CallSid")
            )
            if voicemail_twiml:
                return Response(content=voicemail_twiml, media_type="application/xml")
        if phone_number is None or (phone_number.ai_agent_id is None and route is None):
            # No tenant owns this number, or it has no agent assigned (and no IVR/
            # routing configured for it) — hang up cleanly.
            return _reject_response()

        if await _is_blocked(db, phone_number.tenant_id, from_number):
            return _reject_response()

        tenant = await db.get(Tenant, phone_number.tenant_id)
        if await _check_and_apply_spam_protection(db, tenant, from_number):
            return _reject_response()

        customer, customer_created = await find_or_create_customer_by_phone(
            db, phone_number.tenant_id, from_number
        )
        if customer_created:
            new_customer = customer
        call = Call(
            tenant_id=phone_number.tenant_id,
            customer_id=customer.id,
            ai_agent_id=phone_number.ai_agent_id,
            provider_call_sid=form.get("CallSid"),
            direction=CallDirection.INBOUND,
            status=CallStatus.RINGING,
            from_number=from_number,
            to_number=to_number,
            started_at=datetime.now(UTC),
        )
        db.add(call)
        await db.flush()

    await db.commit()

    if new_customer is not None:
        enqueue_workflow_trigger(
            tenant_id=new_customer.tenant_id,
            trigger=WorkflowTrigger.CUSTOMER_CREATED,
            payload={
                "customer_id": str(new_customer.id),
                "phone": new_customer.phone,
                "source": "call",
            },
        )

    if route is not None:
        routed_twiml = await twiml_for_route(db, route, call)
        await db.commit()
        return Response(content=routed_twiml, media_type="application/xml")

    if call.status == CallStatus.RINGING:
        # Straight to the AI agent: the caller is already talking to it.
        call.status = CallStatus.IN_PROGRESS
        await db.commit()

    twiml = build_stream_twiml(call_id=str(call.id), tenant_id=str(call.tenant_id))
    return Response(content=twiml, media_type="application/xml")


@router.post("/status")
async def status_callback(request: Request, db: AsyncSession = Depends(get_db)) -> Response:
    form = dict((await request.form()).items())

    call_sid = form.get("CallSid")
    result = await db.execute(select(Call).where(Call.provider_call_sid == call_sid))
    call = result.scalar_one_or_none()

    auth_token = await _resolve_auth_token(db, call.tenant_id if call else None)
    await _validate_request(request, form, auth_token=auth_token)

    if call is None:
        return PlainTextResponse("")

    # The realtime gateway's /internal/calls/{id}/end (or /finalize) is
    # authoritative for calls that connected to Media Streams. This handler
    # only finalizes calls that never got that far (no-answer/busy/failed).
    if call.status not in TERMINAL_CALL_STATUSES:
        new_status = _TWILIO_STATUS_MAP.get(form.get("CallStatus", ""))
        if new_status in TERMINAL_CALL_STATUSES:
            duration = form.get("Duration")
            finalized = await finalize_call(
                db,
                call,
                status=new_status,
                duration_seconds=int(duration) if duration and duration.isdigit() else None,
            )
            if finalized:
                await record_missed_call(db, call, form.get("CallStatus"))
        elif new_status is not None:
            call.status = new_status
            await db.commit()

    return PlainTextResponse("")


@router.post("/amd")
async def answering_machine_callback(
    request: Request, db: AsyncSession = Depends(get_db)
) -> Response:
    """Twilio's asynchronous AMD verdict for an outbound AI call (see
    app/services/voicemail_detection.py)."""
    form = dict((await request.form()).items())
    call = (
        await db.execute(select(Call).where(Call.provider_call_sid == form.get("CallSid")))
    ).scalar_one_or_none()
    await _validate_request(
        request, form, auth_token=await _resolve_auth_token(db, call.tenant_id if call else None)
    )
    if call is None or call.status in TERMINAL_CALL_STATUSES:
        return PlainTextResponse("")
    await handle_answered_by(db, call, form.get("AnsweredBy", "unknown"))
    return PlainTextResponse("")


@router.post("/recording")
async def recording_callback(request: Request, db: AsyncSession = Depends(get_db)) -> Response:
    """Twilio posts here when a recording started via start_call_recording
    finishes. Stored as the .mp3 rendition so players can use it directly."""
    form = dict((await request.form()).items())

    call_sid = form.get("CallSid")
    result = await db.execute(select(Call).where(Call.provider_call_sid == call_sid))
    call = result.scalar_one_or_none()

    auth_token = await _resolve_auth_token(db, call.tenant_id if call else None)
    await _validate_request(request, form, auth_token=auth_token)

    recording_url = form.get("RecordingUrl")
    if call is not None and recording_url and form.get("RecordingStatus") == "completed":
        call.recording_url = f"{recording_url}.mp3"
        await db.commit()

    return PlainTextResponse("")
