import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from packages.database.models.crm import CrmStage, CrmTaskStatus, CrmTaskType

# ---- companies -------------------------------------------------------------


class CompanyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    domain: str | None = Field(default=None, max_length=255)
    industry: str | None = Field(default=None, max_length=120)
    phone: str | None = Field(default=None, max_length=32)
    website: str | None = Field(default=None, max_length=255)
    notes: str | None = None


class CompanyUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    domain: str | None = Field(default=None, max_length=255)
    industry: str | None = Field(default=None, max_length=120)
    phone: str | None = Field(default=None, max_length=32)
    website: str | None = Field(default=None, max_length=255)
    notes: str | None = None


class CompanyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    domain: str | None
    industry: str | None
    phone: str | None
    website: str | None
    notes: str | None
    created_at: datetime
    updated_at: datetime


# ---- leads -----------------------------------------------------------------


class LeadCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    email: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=32)
    company_id: uuid.UUID | None = None
    customer_id: uuid.UUID | None = None
    owner_user_id: uuid.UUID | None = None
    stage: CrmStage = CrmStage.NEW_LEAD
    source: str | None = Field(default=None, max_length=80)
    tags: list[str] = Field(default_factory=list)
    custom_fields: dict[str, Any] = Field(default_factory=dict)
    value: Decimal = Field(default=Decimal(0), ge=0, max_digits=14, decimal_places=2)
    currency: str = Field(default="INR", min_length=3, max_length=3)
    score: int | None = Field(default=None, ge=0, le=100)


class LeadUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    email: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=32)
    company_id: uuid.UUID | None = None
    customer_id: uuid.UUID | None = None
    owner_user_id: uuid.UUID | None = None
    source: str | None = Field(default=None, max_length=80)
    tags: list[str] | None = None
    custom_fields: dict[str, Any] | None = None
    value: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    score: int | None = Field(default=None, ge=0, le=100)


class LeadMove(BaseModel):
    stage: CrmStage
    lost_reason: str | None = Field(default=None, max_length=255)


class LeadRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    email: str | None
    phone: str | None
    company_id: uuid.UUID | None
    customer_id: uuid.UUID | None
    owner_user_id: uuid.UUID | None
    source_call_id: uuid.UUID | None
    stage: CrmStage
    stage_changed_at: datetime
    lost_reason: str | None
    source: str | None
    tags: list[str]
    custom_fields: dict[str, Any]
    value: Decimal
    currency: str
    score: int
    score_breakdown: list[Any]
    scored_at: datetime | None
    created_at: datetime
    updated_at: datetime


class PipelineStage(BaseModel):
    stage: CrmStage
    count: int
    total_value: Decimal
    leads: list[LeadRead]


class PipelineRead(BaseModel):
    stages: list[PipelineStage]
    total_leads: int
    total_value: Decimal


class ImportSkipped(BaseModel):
    row: int
    reason: str


class ImportResult(BaseModel):
    created: int
    skipped: list[ImportSkipped]


class RescoreResult(BaseModel):
    rescored: int


class TimelineItem(BaseModel):
    type: str  # note | stage_change | task | call
    id: uuid.UUID
    at: datetime
    title: str
    body: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


# ---- deals -----------------------------------------------------------------


class DealCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    lead_id: uuid.UUID | None = None
    company_id: uuid.UUID | None = None
    customer_id: uuid.UUID | None = None
    owner_user_id: uuid.UUID | None = None
    stage: CrmStage = CrmStage.QUALIFIED
    value: Decimal = Field(default=Decimal(0), ge=0, max_digits=14, decimal_places=2)
    currency: str = Field(default="INR", min_length=3, max_length=3)
    expected_close_date: date | None = None


class DealUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    lead_id: uuid.UUID | None = None
    company_id: uuid.UUID | None = None
    customer_id: uuid.UUID | None = None
    owner_user_id: uuid.UUID | None = None
    stage: CrmStage | None = None
    value: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    expected_close_date: date | None = None


class DealRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    lead_id: uuid.UUID | None
    company_id: uuid.UUID | None
    customer_id: uuid.UUID | None
    owner_user_id: uuid.UUID | None
    stage: CrmStage
    value: Decimal
    currency: str
    expected_close_date: date | None
    closed_at: datetime | None
    created_at: datetime
    updated_at: datetime


# ---- tasks -----------------------------------------------------------------


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    description: str | None = None
    type: CrmTaskType = CrmTaskType.TODO
    due_at: datetime | None = None
    assignee_user_id: uuid.UUID | None = None
    lead_id: uuid.UUID | None = None
    deal_id: uuid.UUID | None = None
    customer_id: uuid.UUID | None = None


class TaskUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    type: CrmTaskType | None = None
    status: CrmTaskStatus | None = None
    due_at: datetime | None = None
    assignee_user_id: uuid.UUID | None = None


class TaskRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    description: str | None
    type: CrmTaskType
    status: CrmTaskStatus
    due_at: datetime | None
    completed_at: datetime | None
    assignee_user_id: uuid.UUID | None
    lead_id: uuid.UUID | None
    deal_id: uuid.UUID | None
    customer_id: uuid.UUID | None
    created_by_ai: bool
    created_at: datetime
    updated_at: datetime


# ---- notes -----------------------------------------------------------------


class NoteCreate(BaseModel):
    body: str = Field(min_length=1, max_length=10000)
    lead_id: uuid.UUID | None = None
    deal_id: uuid.UUID | None = None


class NoteRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: str
    body: str
    author_user_id: uuid.UUID | None
    lead_id: uuid.UUID | None
    deal_id: uuid.UUID | None
    created_at: datetime
