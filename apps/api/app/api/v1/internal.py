"""Endpoints called only by the Go realtime gateway (service-to-service, see
app/core/internal_auth.py) — never by a browser/mobile client. tenant_id is
always derived from the Call row looked up by call_id, never from the
request body.
"""

import json
import logging
import uuid
from datetime import UTC, datetime

from app.core.config import get_settings
from app.core.deps import get_db
from app.core.internal_auth import require_internal_service
from app.integrations.credentials import get_sarvam_api_key, get_twilio_credentials
from app.integrations.twilio_client import (
    MAX_RING_TARGETS,
    CallNotLiveError,
    TwilioClientError,
    build_hangup_twiml,
    build_transfer_twiml,
    redirect_call,
    start_call_recording,
)
from app.integrations.twilio_workspace import build_callback_twiml, build_voicemail_twiml
from app.schemas.internal import (
    CallConfigResponse,
    CallEndRequest,
    CallEndResponse,
    CallFinalizeRequest,
    CallFinalizeResponse,
    CallTurnRequest,
    CallTurnResponse,
)
from app.services import plans
from app.services.agent_engine import run_turn, run_turn_stream
from app.services.call_closing import caller_wants_to_end, conversation_is_over
from app.services.call_finalize import finalize_call
from app.services.routing import route_call
from app.services.webhooks import enqueue_webhook_event
from app.services.workspace import is_human_controlled
from app.workflows.dispatch import enqueue_workflow_trigger
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from twilio.base.exceptions import TwilioException

from packages.database.models import (
    AIAgent,
    Call,
    CallEvent,
    CallStatus,
    Conversation,
    ConversationChannel,
    Message,
    MessageDirection,
    MessageSender,
    Tenant,
    TransferTarget,
    WorkflowTrigger,
)
from packages.database.session import session_scope, set_tenant_context

router = APIRouter(
    prefix="/internal",
    tags=["internal"],
    dependencies=[Depends(require_internal_service)],
)

logger = logging.getLogger(__name__)


async def _get_call(db: AsyncSession, call_id: uuid.UUID) -> Call:
    call = await db.get(Call, call_id)
    if call is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Call not found")
    # Everything else in an internal request is scoped to this call's tenant.
    await set_tenant_context(db, call.tenant_id)
    return call


async def _start_recording_if_enabled(db: AsyncSession, call: Call) -> None:
    """Best-effort: a recording failure must never stop the call itself."""
    if not call.provider_call_sid or call.recording_url:
        return
    tenant = await db.get(Tenant, call.tenant_id)
    if tenant is None or not tenant.call_recording_enabled:
        return
    if not plans.effective(tenant).recording:  # e.g. downgraded after enabling it
        return
    try:
        credentials = await get_twilio_credentials(db, call.tenant_id)
        start_call_recording(
            call_sid=call.provider_call_sid,
            status_callback_url=(
                f"{get_settings().public_base_url}/api/v1/webhooks/telephony/twilio/recording"
            ),
            credentials=credentials,
        )
    except (TwilioClientError, TwilioException):
        logger.warning("Failed to start recording for call %s", call.id, exc_info=True)


async def _get_or_create_conversation(db: AsyncSession, call: Call) -> Conversation:
    result = await db.execute(select(Conversation).where(Conversation.call_id == call.id))
    conversation = result.scalar_one_or_none()
    if conversation is not None:
        return conversation

    conversation = Conversation(
        tenant_id=call.tenant_id,
        customer_id=call.customer_id,
        channel=ConversationChannel.VOICE,
        call_id=call.id,
    )
    db.add(conversation)
    await db.flush()
    return conversation


@router.get("/calls/{call_id}/config", response_model=CallConfigResponse)
async def call_config(call_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> CallConfigResponse:
    """Called once by the Go gateway when a call starts, so STT/TTS use the
    owning tenant's own Sarvam key if they've connected one (see
    app/integrations/credentials.py), and the gateway enforces this agent's
    own call-control policy instead of hardcoded defaults."""
    call = await _get_call(db, call_id)
    agent = await db.get(AIAgent, call.ai_agent_id) if call.ai_agent_id else None
    sarvam_api_key = await get_sarvam_api_key(db, call.tenant_id)
    await _start_recording_if_enabled(db, call)
    enqueue_webhook_event(
        tenant_id=call.tenant_id,
        event="call.started",
        data={
            "call_id": str(call.id),
            "customer_id": str(call.customer_id) if call.customer_id else None,
            "ai_agent_id": str(call.ai_agent_id) if call.ai_agent_id else None,
            "direction": call.direction.value,
            "from_number": call.from_number,
            "to_number": call.to_number,
        },
    )

    if agent is None:
        return CallConfigResponse(
            sarvam_api_key=sarvam_api_key,
            language="en-IN",
            voice="priya",
            greeting="",
            max_call_duration_seconds=await plans.effective_max_call_seconds(
                db, call.tenant_id, 300
            ),
            max_duration_message=(
                "I'm sorry, but I need to end our call now. Thank you for calling."
            ),
            no_response_timeout_seconds=6,
            max_no_response_retries=2,
            no_response_prompt="Sorry, I didn't catch that — could you repeat it?",
            no_response_end_message="I'm still not hearing anything, so I'll end the call here.",
        )

    return CallConfigResponse(
        sarvam_api_key=sarvam_api_key,
        language=agent.language,
        voice=agent.voice or "priya",
        greeting=agent.greeting or "",
        # The agent's own cap, the plan's per-call cap and the minutes left in the
        # plan's allowance, whichever is smallest (see services/plans.py).
        max_call_duration_seconds=await plans.effective_max_call_seconds(
            db, call.tenant_id, agent.max_call_duration_seconds
        ),
        max_duration_message=agent.max_duration_message,
        no_response_timeout_seconds=agent.no_response_timeout_seconds,
        max_no_response_retries=agent.max_no_response_retries,
        no_response_prompt=agent.no_response_prompt,
        no_response_end_message=agent.no_response_end_message,
    )


async def _prepare_call_turn(
    db: AsyncSession, call_id: uuid.UUID, payload: CallTurnRequest
) -> tuple[CallTurnResponse | None, Call | None, AIAgent | None, Conversation | None]:
    """Everything before the LLM runs. Returns (early_response, call, agent,
    conversation): a non-None early_response means no AI turn should happen."""
    call = await _get_call(db, call_id)
    if await is_human_controlled(db, call.id):
        # A human agent took this call over (see app/api/v1/workspace.py): the AI
        # must stay silent. The caller's words are still kept in the transcript.
        conversation = await _get_or_create_conversation(db, call)
        db.add(
            Message(
                tenant_id=call.tenant_id,
                conversation_id=conversation.id,
                direction=MessageDirection.INBOUND,
                sender=MessageSender.CUSTOMER,
                text=payload.transcript,
            )
        )
        await db.commit()
        return CallTurnResponse(reply_text="", action="none", reason="human_controlled"), None, None, None
    if call.ai_agent_id is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Call has no AI agent assigned")

    agent = await db.get(AIAgent, call.ai_agent_id)
    if agent is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Assigned AI agent no longer exists")

    # Checked before spending an LLM call: an agent that's already run past
    # its owner-configured time limit shouldn't get another turn at all.
    elapsed = (datetime.now(UTC) - call.started_at).total_seconds() if call.started_at else 0
    max_seconds = await plans.effective_max_call_seconds(
        db, call.tenant_id, agent.max_call_duration_seconds
    )
    if elapsed >= max_seconds:
        early = CallTurnResponse(
            reply_text=agent.max_duration_message, action="hangup", reason="max_duration_exceeded"
        )
        return early, None, None, None

    conversation = await _get_or_create_conversation(db, call)
    return None, call, agent, conversation


def _finish_call_turn(
    call: Call, conversation: Conversation, payload: CallTurnRequest, result
) -> CallTurnResponse:
    """After the reply is saved and committed: fire the triggers and webhooks and
    shape the response."""
    enqueue_workflow_trigger(
        tenant_id=call.tenant_id,
        trigger=WorkflowTrigger.MESSAGE_RECEIVED,
        payload={
            "conversation_id": str(conversation.id),
            "customer_id": str(conversation.customer_id) if conversation.customer_id else None,
            "channel": ConversationChannel.VOICE.value,
            "text": payload.transcript,
        },
    )

    enqueue_webhook_event(
        tenant_id=call.tenant_id,
        event="message.received",
        data={
            "call_id": str(call.id),
            "conversation_id": str(conversation.id),
            "channel": ConversationChannel.VOICE.value,
            "text": payload.transcript,
            "reply_text": result.reply_text,
        },
    )

    if result.transfer_requested:
        return CallTurnResponse(
            reply_text=result.reply_text,
            action="transfer",
            reason=result.transfer_reason,
            reply_message_id=result.reply_message_id,
        )
    if conversation_is_over(transcript=payload.transcript, reply=result.reply_text):
        # The agent has said goodbye to a caller who is done: end the call once
        # that goodbye has played, instead of waiting for the caller to hang up.
        return CallTurnResponse(
            reply_text=result.reply_text,
            action="hangup",
            reason="caller_requested"
            if caller_wants_to_end(payload.transcript)
            else "conversation_complete",
            reply_message_id=result.reply_message_id,
        )
    return CallTurnResponse(
        reply_text=result.reply_text, action="none", reply_message_id=result.reply_message_id
    )


@router.post("/calls/{call_id}/turn", response_model=CallTurnResponse)
async def call_turn(
    call_id: uuid.UUID, payload: CallTurnRequest, db: AsyncSession = Depends(get_db)
) -> CallTurnResponse:
    early, call, agent, conversation = await _prepare_call_turn(db, call_id, payload)
    if early is not None:
        return early
    result = await run_turn(
        db=db,
        tenant_id=call.tenant_id,
        agent=agent,
        conversation=conversation,
        customer_text=payload.transcript,
    )
    await db.commit()
    return _finish_call_turn(call, conversation, payload, result)


@router.post("/calls/{call_id}/turn/stream")
async def call_turn_stream(call_id: uuid.UUID, payload: CallTurnRequest) -> StreamingResponse:
    """Same turn as POST /calls/{id}/turn, but the reply is streamed as it is
    written so the gateway can start speaking after the first sentence instead
    of after the whole reply. Newline-delimited JSON:

      {"type": "sentence", "text": "..."}        one per speakable sentence
      {"type": "done", ...CallTurnResponse}      always last; reply_text is the full reply
      {"type": "error", "detail": "..."}         instead of "done" if the turn failed

    With nothing streamed (human-controlled call, max duration reached, an agent
    with guardrails) only the "done" line is sent and the gateway speaks its
    reply_text, exactly like the non-streaming endpoint."""

    async def events():
        try:
            async with session_scope() as db:
                early, call, agent, conversation = await _prepare_call_turn(db, call_id, payload)
                if early is not None:
                    yield _line({"type": "done", **early.model_dump(mode="json")})
                    return
                result = None
                async for kind, *rest in run_turn_stream(
                    db=db,
                    tenant_id=call.tenant_id,
                    agent=agent,
                    conversation=conversation,
                    customer_text=payload.transcript,
                ):
                    if kind == "sentence":
                        yield _line({"type": "sentence", "text": rest[0]})
                    else:
                        result = rest[0]
                await db.commit()
                response = _finish_call_turn(call, conversation, payload, result)
                yield _line({"type": "done", **response.model_dump(mode="json")})
        except HTTPException as exc:
            yield _line({"type": "error", "status": exc.status_code, "detail": str(exc.detail)})
        except Exception:
            logger.exception("streaming turn failed for call %s", call_id)
            yield _line({"type": "error", "status": 500, "detail": "turn failed"})

    return StreamingResponse(
        events(), media_type="application/x-ndjson", headers={"Cache-Control": "no-store"}
    )


def _line(event: dict) -> bytes:
    return (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")


@router.delete("/calls/{call_id}/messages/{message_id}", status_code=status.HTTP_204_NO_CONTENT)
async def retract_unspoken_reply(
    call_id: uuid.UUID, message_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> None:
    """The gateway generated an AI reply but the caller spoke over it before it
    was played. Remove it so it never appears in the transcript or in the
    history the LLM reads next turn. Only AI replies of this call can go."""
    call = await _get_call(db, call_id)
    message = await db.get(Message, message_id)
    conversation = await _get_or_create_conversation(db, call)
    if (
        message is None
        or message.conversation_id != conversation.id
        or message.sender != MessageSender.AI_AGENT
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Reply not found")
    await db.delete(message)
    await db.commit()


@router.post("/calls/{call_id}/finalize", response_model=CallFinalizeResponse)
async def call_finalize(
    call_id: uuid.UUID, payload: CallFinalizeRequest, db: AsyncSession = Depends(get_db)
) -> CallFinalizeResponse:
    """Called by the gateway AFTER it finishes speaking reply_text for a
    "transfer"/"hangup" turn (see CallTurnResponse.action) — the gateway
    controls the timing so the caller always hears the message before the
    call actually moves, rather than the backend redirecting the live call
    out from under audio that's still queued to play."""
    call = await _get_call(db, call_id)

    if payload.action == "transfer":
        decision = await route_call(db, call, payload.reason)
        if decision.action == "ring":
            targets = [t.phone_number for t in decision.targets]
        elif decision.action == "legacy":
            targets = (
                (
                    await db.execute(
                        select(TransferTarget.phone_number)
                        .where(
                            TransferTarget.tenant_id == call.tenant_id,
                            TransferTarget.is_active.is_(True),
                        )
                        .order_by(TransferTarget.priority, TransferTarget.created_at)
                        .limit(MAX_RING_TARGETS)
                    )
                )
                .scalars()
                .all()
            )
        else:
            targets = []
        db.add(
            CallEvent(
                tenant_id=call.tenant_id,
                call_id=call.id,
                type="TRANSFER_REQUESTED",
                payload={
                    "reason": payload.reason,
                    "targets": len(targets),
                    "routing": decision.action,
                    "routing_reason": decision.reason,
                },
            )
        )
        await db.commit()
        if not call.summary:
            _enqueue_handoff_summary(call)
        if decision.action == "voicemail":
            twiml = build_voicemail_twiml(decision.message)
        elif decision.action == "callback":
            twiml = build_callback_twiml(decision.message)
        else:
            twiml = build_transfer_twiml(list(targets))
        await _redirect_or_log(db, call, twiml)
        return CallFinalizeResponse(call_id=call.id, status=call.status)

    await _redirect_or_log(db, call, build_hangup_twiml())
    await finalize_call(db, call, status=CallStatus.COMPLETED, reason=payload.reason)
    return CallFinalizeResponse(call_id=call.id, status=call.status)


def _enqueue_handoff_summary(call: Call) -> None:
    """So the human who picks up has an AI summary (best effort, in the background)."""
    try:
        from app.worker.tasks import analyze_call_task

        analyze_call_task.delay(str(call.id))
    except Exception:
        logger.warning("failed to enqueue handoff summary for %s", call.id, exc_info=True)


async def _redirect_or_log(db: AsyncSession, call: Call, twiml: str) -> None:
    if not call.provider_call_sid:
        return
    try:
        credentials = await get_twilio_credentials(db, call.tenant_id)
        redirect_call(call_sid=call.provider_call_sid, twiml=twiml, credentials=credentials)
    except CallNotLiveError:
        logger.info("call %s already ended on Twilio; nothing to redirect", call.id)
    except (TwilioClientError, TwilioException):
        logger.warning("Failed to redirect call %s", call.id, exc_info=True)


@router.post("/calls/{call_id}/end", response_model=CallEndResponse)
async def call_end(
    call_id: uuid.UUID, payload: CallEndRequest, db: AsyncSession = Depends(get_db)
) -> CallEndResponse:
    """The gateway's own signal that Twilio ended the call naturally (the
    caller hung up) — as opposed to /finalize, which is the platform
    proactively ending a still-live call."""
    call = await _get_call(db, call_id)
    if payload.recording_url:
        call.recording_url = payload.recording_url
    if await is_human_controlled(db, call.id):
        # The caller was moved into a human-handled conference, which ends the
        # media stream; the call is finalized when that conference ends instead.
        await db.commit()
        return CallEndResponse(call_id=call.id, status=call.status)
    await finalize_call(db, call, status=payload.status, duration_seconds=payload.duration_seconds)
    return CallEndResponse(call_id=call.id, status=call.status)
