import uuid
from typing import Literal

from pydantic import BaseModel

from packages.database.models import CallStatus


class CallTurnRequest(BaseModel):
    transcript: str


class CallTurnResponse(BaseModel):
    reply_text: str
    # "none": just play reply_text. "transfer"/"hangup": play reply_text,
    # THEN call POST /calls/{id}/finalize with this action — the gateway
    # controls that sequencing so the caller always hears the message before
    # the call moves, unlike naively redirecting the call from here.
    action: Literal["none", "transfer", "hangup"] = "none"
    reason: str | None = None
    # The saved AI message. If the gateway drops the reply unspoken (the caller
    # talked over it), it retracts this id so the transcript and the LLM's
    # history only contain what the caller actually heard.
    reply_message_id: uuid.UUID | None = None


class CallEndRequest(BaseModel):
    status: CallStatus
    duration_seconds: int | None = None
    recording_url: str | None = None


class CallEndResponse(BaseModel):
    call_id: uuid.UUID
    status: CallStatus


class CallFinalizeRequest(BaseModel):
    action: Literal["transfer", "hangup"]
    reason: str | None = None


class CallFinalizeResponse(BaseModel):
    call_id: uuid.UUID
    status: CallStatus


class CallConfigResponse(BaseModel):
    """What the Go gateway needs at call-start, resolved per-tenant (BYO
    Sarvam key if the tenant connected one, else the platform's; call-control
    policy from the assigned AI agent — all owner-configurable)."""

    sarvam_api_key: str
    language: str
    voice: str
    greeting: str
    max_call_duration_seconds: int
    max_duration_message: str
    no_response_timeout_seconds: int
    max_no_response_retries: int
    no_response_prompt: str
    no_response_end_message: str
