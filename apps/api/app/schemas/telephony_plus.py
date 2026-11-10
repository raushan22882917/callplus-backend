import re
import uuid
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from app.services.dialer import DIALER_MODES
from app.services.ivr import QUEUE_STRATEGIES, RING_STRATEGIES, validate_options
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_E164 = re.compile(r"^\+[1-9]\d{6,14}$")
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _e164(value: str) -> str:
    value = value.strip()
    if not _E164.match(value):
        raise ValueError("must be E.164, e.g. +919876543210")
    return value


def _e164_list(values: list[str]) -> list[str]:
    return [_e164(v) for v in values]


def _hhmm(value: str) -> str:
    if not _HHMM.match(value):
        raise ValueError("must be HH:MM (24h)")
    return value


class _Read(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- IVR ----------------------------------------------------------------------


class IvrMenuBase(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    number: str | None = None
    greeting_text: str = Field(min_length=1, max_length=2000)
    language: str = Field(default="en-IN", max_length=16)
    options: dict[str, dict] = Field(default_factory=dict)
    is_active: bool = True
    timeout_seconds: int = Field(default=6, ge=2, le=30)
    max_retries: int = Field(default=2, ge=0, le=5)
    after_hours_action: dict | None = None
    after_hours_message: str | None = Field(default=None, max_length=2000)

    @field_validator("number")
    @classmethod
    def _number(cls, v: str | None) -> str | None:
        return None if v is None else _e164(v)

    @field_validator("options")
    @classmethod
    def _options(cls, v: dict) -> dict:
        problems = validate_options(v)
        if problems:
            raise ValueError("; ".join(problems))
        return v


class IvrMenuCreate(IvrMenuBase):
    pass


class IvrMenuUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    number: str | None = None
    greeting_text: str | None = Field(default=None, min_length=1, max_length=2000)
    language: str | None = Field(default=None, max_length=16)
    options: dict[str, dict] | None = None
    is_active: bool | None = None
    timeout_seconds: int | None = Field(default=None, ge=2, le=30)
    max_retries: int | None = Field(default=None, ge=0, le=5)
    after_hours_action: dict | None = None
    after_hours_message: str | None = Field(default=None, max_length=2000)

    @field_validator("number")
    @classmethod
    def _number(cls, v: str | None) -> str | None:
        return None if v is None else _e164(v)

    @field_validator("options")
    @classmethod
    def _options(cls, v: dict | None) -> dict | None:
        if v is not None:
            problems = validate_options(v)
            if problems:
                raise ValueError("; ".join(problems))
        return v


class IvrMenuRead(_Read, IvrMenuBase):
    id: uuid.UUID
    created_at: datetime


# --- queues / ring groups / forwarding ----------------------------------------


class QueueBase(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    strategy: Literal["round_robin", "longest_idle", "simultaneous"] = "round_robin"
    max_wait_seconds: int = Field(default=300, ge=10, le=3600)
    overflow_action: dict = Field(default_factory=lambda: {"action": "voicemail"})
    hold_message: str = Field(default="Please hold, your call is important to us.", max_length=500)
    hold_music_url: str | None = Field(default=None, max_length=512)
    members: list[str] = Field(default_factory=list, max_length=20)

    _members = field_validator("members")(_e164_list)

    @field_validator("strategy")
    @classmethod
    def _strategy(cls, v: str) -> str:
        assert v in QUEUE_STRATEGIES
        return v

    @field_validator("overflow_action")
    @classmethod
    def _overflow(cls, v: dict) -> dict:
        if v.get("action") not in ("voicemail", "forward", "ring_group", "ai_agent", "hangup"):
            raise ValueError("overflow action must be voicemail, forward, ring_group, ai_agent or hangup")
        return v


class QueueCreate(QueueBase):
    pass


class QueueUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    strategy: Literal["round_robin", "longest_idle", "simultaneous"] | None = None
    max_wait_seconds: int | None = Field(default=None, ge=10, le=3600)
    overflow_action: dict | None = None
    hold_message: str | None = Field(default=None, max_length=500)
    hold_music_url: str | None = Field(default=None, max_length=512)
    members: list[str] | None = Field(default=None, max_length=20)

    @field_validator("members")
    @classmethod
    def _members(cls, v: list[str] | None) -> list[str] | None:
        return None if v is None else _e164_list(v)


class QueueRead(_Read, QueueBase):
    id: uuid.UUID
    created_at: datetime


class RingGroupBase(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    members: list[str] = Field(min_length=1, max_length=10)
    strategy: Literal["simultaneous", "sequential"] = "simultaneous"
    timeout_seconds: int = Field(default=20, ge=5, le=60)

    _members = field_validator("members")(_e164_list)

    @field_validator("strategy")
    @classmethod
    def _strategy(cls, v: str) -> str:
        assert v in RING_STRATEGIES
        return v


class RingGroupCreate(RingGroupBase):
    pass


class RingGroupUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    members: list[str] | None = Field(default=None, min_length=1, max_length=10)
    strategy: Literal["simultaneous", "sequential"] | None = None
    timeout_seconds: int | None = Field(default=None, ge=5, le=60)

    @field_validator("members")
    @classmethod
    def _members(cls, v: list[str] | None) -> list[str] | None:
        return None if v is None else _e164_list(v)


class RingGroupRead(_Read, RingGroupBase):
    id: uuid.UUID
    created_at: datetime


class ForwardingRuleBase(BaseModel):
    number: str
    condition: Literal["always", "after_hours"] = "always"
    destination_type: Literal["number", "ring_group", "queue"] = "number"
    destination: str = Field(min_length=1, max_length=64)
    priority: int = Field(default=100, ge=0, le=10000)
    is_active: bool = True

    _number = field_validator("number")(_e164)

    @model_validator(mode="after")
    def _check_destination(self) -> "ForwardingRuleBase":
        if self.destination_type == "number":
            _e164(self.destination)
        else:
            uuid.UUID(self.destination)
        return self


class ForwardingRuleCreate(ForwardingRuleBase):
    pass


class ForwardingRuleUpdate(BaseModel):
    condition: Literal["always", "after_hours"] | None = None
    destination: str | None = Field(default=None, min_length=1, max_length=64)
    priority: int | None = Field(default=None, ge=0, le=10000)
    is_active: bool | None = None


class ForwardingRuleRead(_Read, ForwardingRuleBase):
    id: uuid.UUID
    created_at: datetime


# --- number routing / SIP --------------------------------------------------------


class NumberRoutingBase(BaseModel):
    e164_number: str
    department: str | None = Field(default=None, max_length=120)
    target_type: Literal["ivr", "ai_agent", "queue", "ring_group"]
    target_id: uuid.UUID | None = None

    _number = field_validator("e164_number")(_e164)


class NumberRoutingCreate(NumberRoutingBase):
    pass


class NumberRoutingUpdate(BaseModel):
    department: str | None = Field(default=None, max_length=120)
    target_type: Literal["ivr", "ai_agent", "queue", "ring_group"] | None = None
    target_id: uuid.UUID | None = None


class NumberRoutingRead(_Read, NumberRoutingBase):
    id: uuid.UUID
    created_at: datetime


class SipTrunkBase(BaseModel):
    label: str = Field(min_length=1, max_length=120)
    provider: str = Field(default="twilio", max_length=60)
    termination_uri: str = Field(min_length=3, max_length=255)
    credentials_ref: str | None = Field(default=None, max_length=120)
    caller_id: str | None = None
    origination_uri: str | None = Field(default=None, max_length=255)
    is_active: bool = True

    @field_validator("caller_id")
    @classmethod
    def _caller_id(cls, v: str | None) -> str | None:
        return None if v is None else _e164(v)


class SipTrunkCreate(SipTrunkBase):
    pass


class SipTrunkUpdate(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=120)
    provider: str | None = Field(default=None, max_length=60)
    termination_uri: str | None = Field(default=None, min_length=3, max_length=255)
    credentials_ref: str | None = Field(default=None, max_length=120)
    caller_id: str | None = None
    origination_uri: str | None = Field(default=None, max_length=255)
    is_active: bool | None = None

    @field_validator("caller_id")
    @classmethod
    def _caller_id(cls, v: str | None) -> str | None:
        return None if v is None else _e164(v)


class SipTrunkRead(_Read, SipTrunkBase):
    id: uuid.UUID
    twilio_trunk_sid: str | None
    sync_status: str
    sync_error: str | None
    synced_at: datetime | None
    created_at: datetime


# --- DND ---------------------------------------------------------------------------


class DndCreate(BaseModel):
    e164_number: str
    note: str | None = Field(default=None, max_length=255)

    _number = field_validator("e164_number")(_e164)


class DndRead(_Read):
    id: uuid.UUID
    e164_number: str
    source: str
    note: str | None
    created_at: datetime


class DndCheckRequest(BaseModel):
    e164_number: str
    timezone: str | None = None
    enforce_quiet_hours: bool = True

    _number = field_validator("e164_number")(_e164)


class DndCheckResponse(BaseModel):
    allowed: bool
    reason: str | None


# --- dialer ---------------------------------------------------------------------------


class DialerSettingsBase(BaseModel):
    mode: Literal["preview", "progressive", "power", "predictive"] = "progressive"
    pacing_ratio: float = Field(default=1.5, ge=1.0, le=10.0)
    max_concurrent: int = Field(default=5, ge=1, le=200)
    abandon_rate_cap: float = Field(default=0.03, ge=0.0, le=0.5)
    default_timezone: str = "Asia/Kolkata"
    call_window_start: str = "09:00"
    call_window_end: str = "21:00"
    quiet_hours_start: str = "21:00"
    quiet_hours_end: str = "09:00"
    daily_call_limit: int | None = Field(default=None, ge=1)
    per_customer_daily_cap: int | None = Field(default=None, ge=1, le=50)
    per_customer_weekly_cap: int | None = Field(default=None, ge=1, le=200)
    max_attempts: int = Field(default=3, ge=1, le=20)
    retry_schedule_minutes: list[int] = Field(default_factory=lambda: [60, 240, 1440], max_length=20)
    route_qualified_to_user_id: uuid.UUID | None = None

    _times = field_validator(
        "call_window_start", "call_window_end", "quiet_hours_start", "quiet_hours_end"
    )(_hhmm)

    @field_validator("mode")
    @classmethod
    def _mode(cls, v: str) -> str:
        assert v in DIALER_MODES
        return v

    @field_validator("default_timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except Exception as exc:  # noqa: BLE001
            raise ValueError("unknown IANA timezone") from exc
        return v

    @field_validator("retry_schedule_minutes")
    @classmethod
    def _retry(cls, v: list[int]) -> list[int]:
        if any(m < 1 or m > 60 * 24 * 14 for m in v):
            raise ValueError("retry delays must be between 1 minute and 14 days")
        return v


class DialerSettingsWrite(DialerSettingsBase):
    pass


class DialerSettingsRead(_Read, DialerSettingsBase):
    id: uuid.UUID
    campaign_id: uuid.UUID


class DispositionCreate(BaseModel):
    disposition: Literal[
        "answered",
        "interested",
        "qualified",
        "not_interested",
        "callback",
        "wrong_number",
        "dnc",
        "no_answer",
        "busy",
        "failed",
        "voicemail",
        "abandoned",
    ]
    notes: str | None = Field(default=None, max_length=2000)
    callback_at: datetime | None = None
    qualified: bool = False
    call_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _check_callback(self) -> "DispositionCreate":
        if self.disposition == "callback" and self.callback_at is None:
            raise ValueError("callback disposition requires callback_at")
        return self


class OutcomeRead(_Read):
    id: uuid.UUID
    contact_id: uuid.UUID
    call_id: uuid.UUID | None
    disposition: str
    source: str
    qualified: bool
    notes: str | None
    callback_at: datetime | None
    created_at: datetime


class PreviewContact(BaseModel):
    contact_id: uuid.UUID
    customer_id: uuid.UUID
    name: str
    phone: str | None
    timezone: str
    attempts: int


# --- calls: park / mask -----------------------------------------------------------------


class ParkRequest(BaseModel):
    slot: str | None = Field(default=None, max_length=32)


class ParkedCallRead(_Read):
    id: uuid.UUID
    call_id: uuid.UUID
    slot: str
    parked_at: datetime
    retrieved_at: datetime | None


class UnparkRequest(BaseModel):
    agent_number: str

    _number = field_validator("agent_number")(_e164)


class MaskedBridgeRequest(BaseModel):
    agent_number: str
    customer_number: str
    caller_id: str | None = None

    _a = field_validator("agent_number")(_e164)
    _c = field_validator("customer_number")(_e164)

    @field_validator("caller_id")
    @classmethod
    def _cid(cls, v: str | None) -> str | None:
        return None if v is None else _e164(v)


class MaskedBridgeResponse(BaseModel):
    call_id: uuid.UUID
    provider_call_sid: str
    caller_id: str
