import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Priority = Literal["low", "medium", "high", "urgent"]
AppointmentStatus = Literal["booked", "confirmed", "cancelled", "completed", "no_show"]
AppointmentSource = Literal["manual", "ai_agent", "whatsapp", "web", "workflow"]
ResourceKind = Literal["staff", "chair", "room"]
FollowUpChannel = Literal["call", "whatsapp", "sms", "email"]
FollowUpStatus = Literal["pending", "done", "failed", "cancelled"]
PromiseStatus = Literal["pending", "kept", "broken", "cancelled"]


class _Read(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---- categories -----------------------------------------------------------


class CategoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    department: str = Field(default="General", min_length=1, max_length=120)
    description: str | None = None


class CategoryUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    department: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = None
    is_active: bool | None = None


class CategoryRead(_Read):
    id: uuid.UUID
    name: str
    department: str
    description: str | None
    is_active: bool


# ---- SLA ------------------------------------------------------------------


class _PolicyBase(BaseModel):
    first_response_minutes: int = Field(ge=1, le=60 * 24 * 365)
    resolution_minutes: int = Field(ge=1, le=60 * 24 * 365)
    business_hours_only: bool = False
    business_start_hour: int = Field(default=9, ge=0, le=23)
    business_end_hour: int = Field(default=18, ge=1, le=24)
    timezone: str = "UTC"
    is_active: bool = True


class SLAPolicyCreate(_PolicyBase):
    priority: Priority


class SLAPolicyUpdate(BaseModel):
    first_response_minutes: int | None = Field(default=None, ge=1, le=60 * 24 * 365)
    resolution_minutes: int | None = Field(default=None, ge=1, le=60 * 24 * 365)
    business_hours_only: bool | None = None
    business_start_hour: int | None = Field(default=None, ge=0, le=23)
    business_end_hour: int | None = Field(default=None, ge=1, le=24)
    timezone: str | None = None
    is_active: bool | None = None


class SLAPolicyRead(_Read):
    id: uuid.UUID
    priority: str
    first_response_minutes: int
    resolution_minutes: int
    business_hours_only: bool
    business_start_hour: int
    business_end_hour: int
    timezone: str
    is_active: bool


class SLAClock(BaseModel):
    due_at: datetime
    state: Literal["ok", "at_risk", "breached", "met"]
    seconds_remaining: int | None


class TicketSLARead(BaseModel):
    ticket_id: uuid.UUID
    subject: str
    ticket_status: str
    priority: str
    department: str | None
    category_id: uuid.UUID | None
    started_at: datetime
    first_response: SLAClock
    resolution: SLAClock
    breached: bool


class TicketSLACategoryUpdate(BaseModel):
    category_id: uuid.UUID | None


class SLASummaryRow(BaseModel):
    department: str
    priority: str
    total: int
    open: int
    breached: int
    at_risk: int
    met: int
    compliance_pct: float | None


# ---- appointments ---------------------------------------------------------


class ServiceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str | None = None
    duration_minutes: int = Field(ge=5, le=60 * 12)
    price: Decimal = Field(default=Decimal("0"), ge=0)


class ServiceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = None
    duration_minutes: int | None = Field(default=None, ge=5, le=60 * 12)
    price: Decimal | None = Field(default=None, ge=0)
    is_active: bool | None = None


class ServiceRead(_Read):
    id: uuid.UUID
    name: str
    description: str | None
    duration_minutes: int
    price: Decimal
    is_active: bool


def _check_hours(value: dict) -> dict:
    from app.services.operations import validate_working_hours

    try:
        validate_working_hours(value)
    except (ValueError, TypeError, KeyError) as exc:
        raise ValueError(f"invalid working_hours: {exc}") from exc
    return value


def _check_tz(value: str) -> str:
    from zoneinfo import ZoneInfo

    try:
        ZoneInfo(value)
    except Exception as exc:
        raise ValueError(f"unknown timezone {value!r}") from exc
    return value


class ResourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    kind: ResourceKind = "staff"
    working_hours: dict[str, list[list[str]]] = Field(default_factory=dict)
    timezone: str = "UTC"

    _hours = field_validator("working_hours")(_check_hours)
    _tz = field_validator("timezone")(_check_tz)


class ResourceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    kind: ResourceKind | None = None
    working_hours: dict[str, list[list[str]]] | None = None
    timezone: str | None = None
    is_active: bool | None = None

    @field_validator("working_hours")
    @classmethod
    def _hours(cls, value):
        return value if value is None else _check_hours(value)

    @field_validator("timezone")
    @classmethod
    def _tz(cls, value):
        return value if value is None else _check_tz(value)


class ResourceRead(_Read):
    id: uuid.UUID
    name: str
    kind: str
    working_hours: dict
    timezone: str
    is_active: bool


class SlotRead(BaseModel):
    resource_id: uuid.UUID
    resource_name: str
    start: datetime
    end: datetime


class AppointmentBook(BaseModel):
    customer_id: uuid.UUID
    service_id: uuid.UUID
    start_at: datetime
    resource_id: uuid.UUID | None = None
    notes: str | None = None
    source: AppointmentSource = "manual"


class AppointmentReschedule(BaseModel):
    start_at: datetime
    resource_id: uuid.UUID | None = None


class AppointmentCancel(BaseModel):
    reason: str | None = None


class AppointmentStatusUpdate(BaseModel):
    status: Literal["confirmed", "completed", "no_show"]


class AppointmentRead(_Read):
    id: uuid.UUID
    customer_id: uuid.UUID
    service_id: uuid.UUID
    resource_id: uuid.UUID
    start_at: datetime
    end_at: datetime
    status: str
    source: str
    notes: str | None
    cancel_reason: str | None
    reminder_sent_at: datetime | None
    reminder_error: str | None
    customer_name: str | None = None
    service_name: str | None = None
    resource_name: str | None = None


# ---- follow-ups -----------------------------------------------------------


class FollowUpCreate(BaseModel):
    customer_id: uuid.UUID
    channel: FollowUpChannel
    due_at: datetime
    message: str | None = None
    subject: str | None = None
    template_name: str | None = None
    ai_agent_id: uuid.UUID | None = None


class FollowUpUpdate(BaseModel):
    due_at: datetime | None = None
    status: Literal["pending", "cancelled"] | None = None
    message: str | None = None


class FollowUpRead(_Read):
    id: uuid.UUID
    customer_id: uuid.UUID
    channel: str
    due_at: datetime
    status: str
    payload: dict
    created_by: str
    reference_type: str | None
    reference_id: uuid.UUID | None
    attempts: int
    last_error: str | None
    executed_at: datetime | None
    created_at: datetime
    customer_name: str | None = None


# ---- collections ----------------------------------------------------------


class PromiseCreate(BaseModel):
    customer_id: uuid.UUID
    amount: Decimal = Field(gt=0)
    currency: str = Field(default="INR", min_length=3, max_length=3)
    due_date: date
    notes: str | None = None


class PromiseUpdate(BaseModel):
    status: PromiseStatus | None = None
    due_date: date | None = None
    notes: str | None = None


class PromiseRead(_Read):
    id: uuid.UUID
    customer_id: uuid.UUID
    amount: Decimal
    currency: str
    due_date: date
    promised_on: date
    status: str
    notes: str | None
    created_by: str
    customer_name: str | None = None
    overdue: bool = False
    days_overdue: int = 0
