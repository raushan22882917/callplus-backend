import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CsatSettingsRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    enabled: bool
    after_calls: bool
    after_tickets: bool
    channel: Literal["auto", "whatsapp", "sms"]
    delay_minutes: int
    cooldown_hours: int
    expire_hours: int
    min_call_seconds: int
    low_score_ticket_threshold: int
    question: str
    thank_you: str


class CsatSettingsUpdate(BaseModel):
    enabled: bool | None = None
    after_calls: bool | None = None
    after_tickets: bool | None = None
    channel: Literal["auto", "whatsapp", "sms"] | None = None
    delay_minutes: int | None = Field(default=None, ge=0, le=10080)
    cooldown_hours: int | None = Field(default=None, ge=0, le=720)
    expire_hours: int | None = Field(default=None, ge=1, le=720)
    min_call_seconds: int | None = Field(default=None, ge=0, le=3600)
    low_score_ticket_threshold: int | None = Field(default=None, ge=0, le=4)
    question: str | None = Field(default=None, min_length=5, max_length=600)
    thank_you: str | None = Field(default=None, min_length=1, max_length=300)


class CsatSurveyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    customer_id: uuid.UUID
    source_type: str
    source_id: uuid.UUID
    channel: str | None
    status: str
    rating: int | None
    comment: str | None
    error: str | None
    send_at: datetime
    sent_at: datetime | None
    answered_at: datetime | None
