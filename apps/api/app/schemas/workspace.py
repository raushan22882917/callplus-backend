import re
import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from packages.database.models.workspace import HandlerState, HumanAgentStatus

_E164 = re.compile(r"^\+[1-9]\d{6,14}$")
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _e164(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if not _E164.match(value):
        raise ValueError("phone_number must be E.164, e.g. +919876543210")
    return value


def _clean_list(values: list[str]) -> list[str]:
    seen: list[str] = []
    for item in values:
        item = item.strip()
        if item and item.lower() not in {s.lower() for s in seen}:
            seen.append(item[:60])
    return seen[:50]


class AgentProfileRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: uuid.UUID
    status: HumanAgentStatus
    phone_number: str | None
    skills: list[str]
    languages: list[str]
    departments: list[str]
    is_vip_handler: bool
    max_concurrent: int
    status_changed_at: datetime | None


class AgentWithUser(AgentProfileRead):
    full_name: str
    email: str
    active_calls: int = 0


class StatusUpdate(BaseModel):
    status: HumanAgentStatus
    phone_number: str | None = None

    _phone = field_validator("phone_number")(_e164)


class AgentProfileUpdate(BaseModel):
    phone_number: str | None = None
    skills: list[str] | None = None
    languages: list[str] | None = None
    departments: list[str] | None = None
    is_vip_handler: bool | None = None
    max_concurrent: int | None = Field(default=None, ge=1, le=20)

    _phone = field_validator("phone_number")(_e164)

    @field_validator("skills", "languages", "departments")
    @classmethod
    def _lists(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else _clean_list(value)


class BusinessWindow(BaseModel):
    weekday: int = Field(ge=0, le=6)  # Monday = 0
    start: str
    end: str

    @field_validator("start", "end")
    @classmethod
    def _time(cls, value: str) -> str:
        if not _HHMM.match(value):
            raise ValueError("times must be HH:MM (24h)")
        return value

    @model_validator(mode="after")
    def _order(self) -> "BusinessWindow":
        if self.end <= self.start:
            raise ValueError("end must be after start")
        return self


class BusinessHoursPayload(BaseModel):
    timezone: str = Field(default="UTC", max_length=50)
    windows: list[BusinessWindow] = Field(default_factory=list, max_length=70)
    fallback_action: Literal["voicemail", "callback"] = "voicemail"
    fallback_message: str | None = Field(default=None, max_length=500)

    @field_validator("timezone")
    @classmethod
    def _tz(cls, value: str) -> str:
        from zoneinfo import ZoneInfo

        try:
            ZoneInfo(value)
        except Exception as exc:
            raise ValueError("unknown timezone") from exc
        return value


class HolidayCreate(BaseModel):
    day: date
    name: str = Field(default="", max_length=120)


class HolidayRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    day: date
    name: str


class RoutingTargetRead(BaseModel):
    user_id: uuid.UUID
    phone_number: str
    score: int
    full_name: str | None = None


class RoutingPreview(BaseModel):
    action: str
    reason: str
    message: str | None
    targets: list[RoutingTargetRead]


class TransferRequest(BaseModel):
    mode: Literal["cold", "warm"] = "warm"
    user_id: uuid.UUID | None = None
    phone_number: str | None = None

    _phone = field_validator("phone_number")(_e164)

    @model_validator(mode="after")
    def _one_target(self) -> "TransferRequest":
        if (self.user_id is None) == (self.phone_number is None):
            raise ValueError("provide exactly one of user_id or phone_number")
        return self


class ConferenceAddRequest(BaseModel):
    user_id: uuid.UUID | None = None
    phone_number: str | None = None

    _phone = field_validator("phone_number")(_e164)

    @model_validator(mode="after")
    def _one_target(self) -> "ConferenceAddRequest":
        if (self.user_id is None) == (self.phone_number is None):
            raise ValueError("provide exactly one of user_id or phone_number")
        return self


class CallbackRequest(BaseModel):
    note: str | None = Field(default=None, max_length=500)
    priority: Literal["low", "medium", "high", "urgent"] = "medium"


class ControlResult(BaseModel):
    call_id: uuid.UUID
    state: HandlerState
    on_hold: bool
    muted: bool
    detail: str | None = None


class HandoffMessage(BaseModel):
    sender: str
    text: str | None
    at: datetime


class HandoffCustomer(BaseModel):
    id: uuid.UUID
    name: str
    phone: str | None
    email: str | None
    tags: list[str]
    is_vip: bool


class HandoffPreviousCall(BaseModel):
    id: uuid.UUID
    started_at: datetime | None
    duration_seconds: int | None
    summary: str | None
    sentiment: str | None
    outcome: str | None


class HandoffTicket(BaseModel):
    id: uuid.UUID
    subject: str
    status: str
    priority: str
    created_at: datetime


class SuggestedAction(BaseModel):
    label: str
    reason: str


class HandoffPackage(BaseModel):
    call_id: uuid.UUID
    status: str
    direction: str
    from_number: str | None
    to_number: str | None
    started_at: datetime | None
    handler: HandlerState
    handler_user_id: uuid.UUID | None
    customer: HandoffCustomer | None
    transcript: list[HandoffMessage]
    summary: str | None
    sentiment: str | None
    intent: str | None
    outcome: str | None
    action_items: list[str]
    previous_calls: list[HandoffPreviousCall]
    tickets: list[HandoffTicket]
    suggested_next_action: SuggestedAction
    recording_url: str | None


class LiveCall(BaseModel):
    id: uuid.UUID
    status: str
    direction: str
    customer_name: str | None
    from_number: str | None
    started_at: datetime | None
    seconds_elapsed: int
    sentiment: str | None
    intent: str | None
    handler: HandlerState
    handler_user_id: uuid.UUID | None
    handler_name: str | None
    waiting: bool


class SlaBreach(BaseModel):
    ticket_id: uuid.UUID
    subject: str
    priority: str
    age_hours: float
    limit_hours: int


class LiveBoard(BaseModel):
    calls: list[LiveCall]
    ai_count: int
    human_count: int
    waiting_count: int
    agents_by_status: dict[str, int]
    sla_breaches: list[SlaBreach]
    sla_policy_hours: dict[str, int]


class SupervisorJoinRequest(BaseModel):
    phone_number: str | None = None

    _phone = field_validator("phone_number")(_e164)
