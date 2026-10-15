"""Missed calls, voicemail and click-to-call (the voice side of the
omnichannel module). Twilio protocol notes: an unanswered inbound leg ends
with CallStatus no-answer/busy/failed/canceled on the status callback;
<Record> posts RecordingUrl/RecordingDuration to recordingStatusCallback and
the transcript (TranscriptionText) later to transcribeCallback."""

import uuid
from datetime import UTC, datetime

from app.core.config import get_settings
from app.integrations.channel_payloads import build_voicemail_twiml
from app.integrations.credentials import get_twilio_credentials
from app.integrations.twilio_client import TwilioClientError, place_outbound_call
from app.services.channels import get_channel_settings
from app.services.customer_lookup import find_or_create_customer_by_phone
from app.services.outbound_call import OutboundCallError, place_ai_outbound_call
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from twilio.base.exceptions import TwilioException
from twilio.twiml.voice_response import Dial, VoiceResponse

from packages.database.models import Call, CallDirection, CallStatus, PhoneNumber
from packages.database.models.channels import MissedCall, Voicemail

MISSED_TWILIO_STATUSES = {"no-answer": "no-answer", "busy": "busy", "failed": "failed", "canceled": "no-answer"}
DEFAULT_VOICEMAIL_GREETING = (
    "Sorry, we cannot take your call right now. Please leave a message after the beep "
    "and we will call you back."
)


def _base() -> str:
    return f"{get_settings().public_base_url}/api/v1"


async def record_missed_call(
    db: AsyncSession, call: Call, twilio_status: str | None
) -> MissedCall | None:
    """Called from the Twilio status callback once an inbound call has been
    finalized. Only unanswered inbound statuses count; idempotent per call."""
    reason = MISSED_TWILIO_STATUSES.get(twilio_status or "")
    if call.direction != CallDirection.INBOUND or reason is None:
        return None
    existing = (
        await db.execute(select(MissedCall).where(MissedCall.call_id == call.id))
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    missed = MissedCall(
        tenant_id=call.tenant_id,
        call_id=call.id,
        provider_call_sid=call.provider_call_sid,
        customer_id=call.customer_id,
        from_number=call.from_number,
        to_number=call.to_number,
        reason=reason,
    )
    db.add(missed)
    await db.commit()
    return missed


async def voicemail_twiml_for_unassigned_number(
    db: AsyncSession, phone_number: PhoneNumber, *, from_number: str, call_sid: str | None
) -> str | None:
    """TwiML that records a message when a tenant-owned number has no AI agent
    and voicemail is enabled; None when voicemail is off (caller is rejected)."""
    settings = await get_channel_settings(db, phone_number.tenant_id, "voicemail")
    if settings is None or not settings.enabled:
        return None
    customer, _ = await find_or_create_customer_by_phone(db, phone_number.tenant_id, from_number)
    db.add(
        MissedCall(
            tenant_id=phone_number.tenant_id,
            provider_call_sid=call_sid,
            customer_id=customer.id,
            from_number=from_number,
            to_number=phone_number.e164_number,
            reason="voicemail",
        )
    )
    await db.commit()
    return build_voicemail_twiml(
        greeting=(settings.config or {}).get("greeting") or DEFAULT_VOICEMAIL_GREETING,
        recording_callback_url=f"{_base()}/channels/voicemail/recording",
        transcribe_callback_url=f"{_base()}/channels/voicemail/transcription",
    )


async def upsert_voicemail(
    db: AsyncSession,
    missed: MissedCall,
    *,
    recording_sid: str | None,
    recording_url: str | None = None,
    duration: int | None = None,
    transcription: str | None = None,
    transcription_status: str | None = None,
) -> Voicemail:
    voicemail = None
    if recording_sid:
        voicemail = (
            await db.execute(
                select(Voicemail).where(Voicemail.provider_recording_sid == recording_sid)
            )
        ).scalar_one_or_none()
    if voicemail is None:
        voicemail = Voicemail(
            tenant_id=missed.tenant_id,
            missed_call_id=missed.id,
            provider_call_sid=missed.provider_call_sid,
            provider_recording_sid=recording_sid,
            from_number=missed.from_number,
            to_number=missed.to_number,
        )
        db.add(voicemail)
    if recording_url:
        voicemail.recording_url = f"{recording_url}.mp3"[:512]
    if duration is not None:
        voicemail.duration_seconds = duration
    if transcription is not None:
        voicemail.transcription = transcription
    if transcription_status:
        voicemail.transcription_status = transcription_status
    await db.commit()
    await db.refresh(voicemail)
    return voicemail


async def _tenant_from_number(db: AsyncSession, tenant_id: uuid.UUID, from_number: str | None) -> str:
    if from_number:
        return from_number
    numbers = (
        await db.execute(select(PhoneNumber).where(PhoneNumber.tenant_id == tenant_id))
    ).scalars().all()
    if len(numbers) != 1:
        raise OutboundCallError(
            f"from_number is required: this tenant has {len(numbers)} phone number(s), not exactly one"
        )
    return numbers[0].e164_number


async def place_click_to_call(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    to_number: str,
    mode: str,
    ai_agent_id: str | None = None,
    from_number: str | None = None,
    agent_phone: str | None = None,
) -> Call:
    """mode "ai": the AI agent phones the customer (existing outbound flow).
    mode "bridge": rings the staff member's own phone first, then connects
    them to the customer when they pick up."""
    if mode == "ai":
        return await place_ai_outbound_call(
            db, tenant_id, to_number=to_number, ai_agent_id=ai_agent_id, from_number=from_number
        )
    if mode != "bridge":
        raise OutboundCallError("mode must be 'ai' or 'bridge'")
    if not agent_phone:
        raise OutboundCallError("agent_phone is required for a bridged call")

    resolved_from = await _tenant_from_number(db, tenant_id, from_number)
    customer, _ = await find_or_create_customer_by_phone(db, tenant_id, to_number)
    call = Call(
        tenant_id=tenant_id,
        customer_id=customer.id,
        direction=CallDirection.OUTBOUND,
        from_number=resolved_from,
        to_number=to_number,
    )
    db.add(call)
    await db.flush()
    try:
        credentials = await get_twilio_credentials(db, tenant_id)
        call.provider_call_sid = place_outbound_call(
            to=agent_phone,
            from_=resolved_from,
            voice_webhook_url=f"{_base()}/channels/click-to-call/bridge?call_id={call.id}",
            status_callback_url=f"{_base()}/webhooks/telephony/twilio/status",
            credentials=credentials,
        )
    except (TwilioClientError, TwilioException) as exc:
        raise OutboundCallError(str(exc)) from exc
    call.status = CallStatus.RINGING
    await db.commit()
    await db.refresh(call)
    return call


def bridge_twiml(*, customer_number: str, caller_id: str | None) -> str:
    response = VoiceResponse()
    dial = Dial(caller_id=caller_id) if caller_id else Dial()
    dial.number(customer_number)
    response.append(dial)
    return str(response)


def mark_handled(row: MissedCall | Voicemail, user_id: uuid.UUID, handled: bool) -> None:
    row.handled = handled
    row.handled_at = datetime.now(UTC) if handled else None
    row.handled_by_user_id = user_id if handled else None
