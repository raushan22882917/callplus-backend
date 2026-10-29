"""Ends calls that nobody told us had ended.

A call is normally closed by Twilio's status webhook or by the realtime gateway's own "stop" handling.
If both miss (the gateway restarted mid-call, the webhook endpoint was down during a deploy), the call
stays "in progress" forever: it never gets a duration (so its minutes are never billed or counted
against a free plan), never produces a summary, survey or CALL_ENDED automation, and shows as live in
the dashboard. This sweep asks Twilio what really happened and finalizes through the same
`finalize_call` as every other path, so a call is still ended exactly once."""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.integrations.credentials import get_twilio_credentials
from app.integrations.twilio_client import (
    CallNotLiveError,
    TwilioClientError,
    build_hangup_twiml,
    fetch_call_outcome,
    redirect_call,
)
from app.services.call_finalize import finalize_call
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from twilio.base.exceptions import TwilioException

from packages.database.models import AIAgent, Call, CallStatus

logger = logging.getLogger(__name__)

# A call that never connected (queued/ringing) is given a few minutes; one that connected is given the
# agent's own time limit plus a margin before we step in.
UNANSWERED_AFTER = timedelta(minutes=5)
CONNECTED_GRACE = timedelta(minutes=3)
DEFAULT_MAX_CALL = timedelta(minutes=30)
BATCH = 50

_TWILIO_OUTCOME = {
    "completed": CallStatus.COMPLETED,
    "busy": CallStatus.FAILED,
    "failed": CallStatus.FAILED,
    "canceled": CallStatus.FAILED,
    "no-answer": CallStatus.NO_ANSWER,
}


@dataclass
class ReapResult:
    checked: int = 0
    finalized: int = 0
    still_live: int = 0


def _due(call: Call, agent_cap_seconds: int | None, now: datetime) -> bool:
    if call.status == CallStatus.IN_PROGRESS:
        began = call.started_at or call.created_at
        cap = timedelta(seconds=agent_cap_seconds) if agent_cap_seconds else DEFAULT_MAX_CALL
        return now - began > cap + CONNECTED_GRACE
    return now - call.created_at > UNANSWERED_AFTER


async def reap_stuck_calls(db: AsyncSession, *, now: datetime | None = None) -> ReapResult:
    now = now or datetime.now(UTC)
    result = ReapResult()
    # Anything older than the unanswered window is a candidate; _due() applies the per-call limit.
    candidates = (
        (
            await db.execute(
                select(Call)
                .where(
                    Call.status.in_([CallStatus.QUEUED, CallStatus.RINGING, CallStatus.IN_PROGRESS]),
                    Call.created_at < now - UNANSWERED_AFTER,
                )
                .order_by(Call.created_at)
                .limit(BATCH * 4)
            )
        )
        .scalars()
        .all()
    )
    for call in candidates:
        if result.checked >= BATCH:
            break
        cap = None
        if call.ai_agent_id:
            agent = await db.get(AIAgent, call.ai_agent_id)
            cap = agent.max_call_duration_seconds if agent else None
        if not _due(call, cap, now):
            continue
        result.checked += 1
        await _settle(db, call, result)
    return result


async def _settle(db: AsyncSession, call: Call, result: ReapResult) -> None:
    outcome: CallStatus | None = None
    duration: int | None = None
    reason = "stale_call_reaped"
    live = False
    # Twilio's Call SIDs all start with "CA"; anything else went out through Exotel, which reports
    # the outcome to its own status callback, so asking Twilio would only fail every sweep.
    if call.provider_call_sid and call.provider_call_sid.startswith("CA"):
        try:
            credentials = await get_twilio_credentials(db, call.tenant_id)
            twilio_status, duration = fetch_call_outcome(
                call_sid=call.provider_call_sid, credentials=credentials
            )
            outcome = _TWILIO_OUTCOME.get(twilio_status)
            live = outcome is None  # queued / ringing / in-progress on Twilio's side
            reason = f"twilio_reported_{twilio_status}"
        except CallNotLiveError:
            reason = "twilio_does_not_know_this_call"
        except (TwilioClientError, TwilioException):
            logger.warning("could not ask Twilio about call %s; will retry next sweep", call.id, exc_info=True)
            return
    if live:
        # Still running far past its limit: hang it up so it stops costing money, then close it.
        try:
            redirect_call(
                call_sid=call.provider_call_sid,
                twiml=build_hangup_twiml(),
                credentials=await get_twilio_credentials(db, call.tenant_id),
            )
        except (CallNotLiveError, TwilioClientError, TwilioException):
            logger.info("hang-up of overdue call %s did not go through", call.id, exc_info=True)
        outcome, reason = CallStatus.COMPLETED, "overdue_call_hung_up"
        result.still_live += 1
    if outcome is None:
        # No usable answer from Twilio: a call that was talking counts as completed, one that never
        # started as failed.
        outcome = CallStatus.COMPLETED if call.started_at else CallStatus.FAILED
    if await finalize_call(db, call, status=outcome, reason=reason, duration_seconds=duration):
        result.finalized += 1
