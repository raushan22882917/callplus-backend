"""Ends a Call row exactly once, regardless of which of three independent
paths notices the call is over first: the caller hanging up (Twilio's status
webhook, app/api/v1/telephony.py), the realtime gateway's own Twilio "stop"
event (app/api/v1/internal.py's /end), or the platform proactively hanging
up (max call duration / no-response exhausted, .../finalize). All three call
this instead of writing to the Call row directly, so CALL_ENDED workflows
fire exactly once no matter which path wins the race.
"""

import logging
from datetime import UTC, datetime

from app.services import cost_controls, csat
from app.services.webhooks import enqueue_webhook_event
from app.workflows.dispatch import enqueue_workflow_trigger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import (
    Call,
    CallEvent,
    CallStatus,
    Conversation,
    Message,
    MessageSender,
    WorkflowTrigger,
)

logger = logging.getLogger(__name__)

TERMINAL_CALL_STATUSES = {CallStatus.COMPLETED, CallStatus.FAILED, CallStatus.NO_ANSWER}


async def finalize_call(
    db: AsyncSession,
    call: Call,
    *,
    status: CallStatus,
    reason: str | None = None,
    duration_seconds: int | None = None,
) -> bool:
    """Returns False (no-op) if the call was already finalized."""
    if call.status in TERMINAL_CALL_STATUSES:
        return False

    call.status = status
    call.ended_at = datetime.now(UTC)
    if call.started_at:
        call.duration_seconds = int((call.ended_at - call.started_at).total_seconds())
    elif duration_seconds is not None:
        call.duration_seconds = duration_seconds

    db.add(
        CallEvent(
            tenant_id=call.tenant_id,
            call_id=call.id,
            type="CALL_ENDED",
            payload={
                "status": status.value,
                "reason": reason,
                "duration_seconds": call.duration_seconds,
            },
        )
    )
    await db.commit()

    # Enqueued after commit, not run inline: a workflow action can call
    # WhatsApp/Razorpay, and whichever request finalized the call shouldn't
    # wait on that.
    enqueue_workflow_trigger(
        tenant_id=call.tenant_id,
        trigger=WorkflowTrigger.CALL_ENDED,
        payload={
            "call_id": str(call.id),
            "customer_id": str(call.customer_id) if call.customer_id else None,
            "status": status.value,
            "duration_seconds": call.duration_seconds,
        },
    )
    await emit_call_webhooks(db, call, status)
    _enqueue_analysis(call)
    _enqueue_qa_scoring(call)
    if status == CallStatus.COMPLETED:
        await csat.schedule_after_call(db, call)
    # The call's minutes now count toward the month: raise a budget alert if
    # this pushed spend over a threshold.
    await cost_controls.evaluate_alerts_safely(db, call.tenant_id)
    return True


async def emit_call_webhooks(db: AsyncSession, call: Call, status: CallStatus) -> None:
    """call.ended, then call.transcript_ready with the full transcript. Best
    effort: a webhook problem must never undo or fail finalizing the call."""
    try:
        base = {
            "call_id": str(call.id),
            "customer_id": str(call.customer_id) if call.customer_id else None,
            "ai_agent_id": str(call.ai_agent_id) if call.ai_agent_id else None,
            "direction": call.direction.value,
            "from_number": call.from_number,
            "to_number": call.to_number,
            "status": status.value,
            "duration_seconds": call.duration_seconds,
        }
        enqueue_webhook_event(tenant_id=call.tenant_id, event="call.ended", data=base)

        conversation = (
            await db.execute(select(Conversation).where(Conversation.call_id == call.id))
        ).scalar_one_or_none()
        if conversation is None:
            return
        messages = (
            (
                await db.execute(
                    select(Message)
                    .where(Message.conversation_id == conversation.id)
                    .order_by(Message.created_at.asc())
                )
            )
            .scalars()
            .all()
        )
        enqueue_webhook_event(
            tenant_id=call.tenant_id,
            event="call.transcript_ready",
            data={
                "call_id": str(call.id),
                "transcript": [
                    {
                        "speaker": "agent" if m.sender == MessageSender.AI_AGENT else "caller",
                        "text": m.text,
                        "at": m.created_at.isoformat(),
                    }
                    for m in messages
                ],
            },
        )
    except Exception:
        logger.warning("failed to emit call webhooks for %s", call.id, exc_info=True)


def _enqueue_analysis(call: Call) -> None:
    """Summary/sentiment/intent run in the background so hanging up never waits on the LLM."""
    if call.status != CallStatus.COMPLETED:
        return
    try:
        from app.worker.tasks import analyze_call_task

        analyze_call_task.delay(str(call.id))
    except Exception:
        logger.warning("failed to enqueue call analysis for %s", call.id, exc_info=True)


def _enqueue_qa_scoring(call: Call) -> None:
    """AI QA scoring (app/services/qa.py), also in the background."""
    if call.status != CallStatus.COMPLETED:
        return
    try:
        from app.worker.tasks import score_call_qa_task

        score_call_qa_task.delay(str(call.id))
    except Exception:
        logger.warning("failed to enqueue QA scoring for %s", call.id, exc_info=True)
