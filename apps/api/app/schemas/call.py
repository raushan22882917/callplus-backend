import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from packages.database.models import CallDirection, CallStatus


class CallCreate(BaseModel):
    """Creates a call record. For `direction: outbound`, this actually places
    the call via Twilio (to_number, from_number, and an ai_agent_id — the
    agent that should handle it — are all required) and connects it to the
    realtime gateway, same as an inbound call. For `direction: inbound`, this
    just logs a call that happened outside the system — real inbound calls
    arrive through the Twilio webhook (app/api/v1/telephony.py), not here.
    """

    customer_id: uuid.UUID | None = None
    direction: CallDirection
    to_number: str | None = Field(default=None, max_length=32)
    from_number: str | None = Field(default=None, max_length=32)
    ai_agent_id: uuid.UUID | None = None


class CallRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    customer_id: uuid.UUID | None
    ai_agent_id: uuid.UUID | None
    direction: CallDirection
    status: CallStatus
    from_number: str | None
    to_number: str | None
    started_at: datetime | None
    ended_at: datetime | None
    duration_seconds: int | None
    recording_url: str | None
    summary: str | None = None
    sentiment: str | None = None
    intent: str | None = None
    outcome: str | None = None
    action_items: list[str] | None = None
    analyzed_at: datetime | None = None
    created_at: datetime
