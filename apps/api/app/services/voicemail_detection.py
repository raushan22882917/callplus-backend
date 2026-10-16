"""Outbound answering-machine detection (AMD).

place_outbound_call(amd_callback_url=...) makes Twilio classify who answered
and POST it to /webhooks/telephony/twilio/amd. If a machine answered, the call
is ended, or, when the agent has a voicemail_message and the greeting has
finished, that message is spoken and then the call hangs up. Humans are never
touched. Off by default per agent (AIAgent.detect_voicemail).
"""

import logging
import uuid
from xml.sax.saxutils import escape

from app.core.config import get_settings
from app.integrations.credentials import get_twilio_credentials
from app.integrations.twilio_client import (
    CallNotLiveError,
    TwilioClientError,
    build_hangup_twiml,
    redirect_call,
)
from sqlalchemy.ext.asyncio import AsyncSession
from twilio.base.exceptions import TwilioException

from packages.database.models import AIAgent, Call

logger = logging.getLogger(__name__)

KNOWN_VERDICTS = {
    "human",
    "machine_start",
    "machine_end_beep",
    "machine_end_silence",
    "machine_end_other",
    "fax",
    "unknown",
}
# The greeting has finished, so a message spoken now is heard on the recording.
_GREETING_DONE = {"machine_end_beep", "machine_end_silence", "machine_end_other"}


def amd_callback_url() -> str:
    return f"{get_settings().public_base_url}/api/v1/webhooks/telephony/twilio/amd"


async def amd_callback_for_agent(db: AsyncSession, agent_id: uuid.UUID | None) -> str | None:
    """The AMD callback URL to pass to place_outbound_call, or None when the
    agent doesn't want detection."""
    if agent_id is None:
        return None
    agent = await db.get(AIAgent, agent_id)
    return amd_callback_url() if agent is not None and agent.detect_voicemail else None


def _say_and_hangup(message: str) -> str:
    return f'<?xml version="1.0" encoding="UTF-8"?><Response><Say>{escape(message)}</Say><Hangup/></Response>'


async def handle_answered_by(db: AsyncSession, call: Call, answered_by: str) -> str:
    """Record the verdict and act on it. Returns what was done: "none",
    "hangup" or "voicemail"."""
    verdict = answered_by if answered_by in KNOWN_VERDICTS else "unknown"
    call.answered_by = verdict
    if verdict in {"human", "unknown"}:
        await db.commit()
        return "none"

    agent = await db.get(AIAgent, call.ai_agent_id) if call.ai_agent_id else None
    message = (agent.voicemail_message or "").strip() if agent is not None else ""
    leave_message = bool(message) and verdict in _GREETING_DONE
    twiml = _say_and_hangup(message) if leave_message else build_hangup_twiml()
    action = "voicemail" if leave_message else "hangup"
    call.outcome = "voicemail"
    await db.commit()

    if call.provider_call_sid:
        try:
            credentials = await get_twilio_credentials(db, call.tenant_id)
            redirect_call(call_sid=call.provider_call_sid, twiml=twiml, credentials=credentials)
        except CallNotLiveError:
            logger.info("call %s hung up before the AMD verdict could be acted on", call.id)
        except (TwilioClientError, TwilioException):
            logger.warning("failed to act on AMD verdict for call %s", call.id, exc_info=True)
    return action
