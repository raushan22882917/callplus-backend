import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.services.privacy import CONSENT_PURPOSES, REQUEST_KINDS


class ProfileRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    legal_name: str | None
    officer_name: str | None
    officer_title: str
    officer_email: str | None
    officer_phone: str | None
    address: str | None
    purposes: list[str]
    retention_summary: str
    extra_notice: str | None
    notice_version: int
    call_disclosure_enabled: bool
    call_disclosure_text: str
    request_sla_days: int
    notice_lines: list[str] = []
    public_url: str = ""


class ProfileUpdate(BaseModel):
    legal_name: str | None = Field(default=None, max_length=255)
    officer_name: str | None = Field(default=None, max_length=255)
    officer_title: str = Field(default="Grievance Officer", min_length=2, max_length=80)
    officer_email: EmailStr | None = None
    officer_phone: str | None = Field(default=None, max_length=32)
    address: str | None = Field(default=None, max_length=1000)
    purposes: list[str] = Field(default_factory=list, max_length=12)
    retention_summary: str = Field(min_length=10, max_length=1000)
    extra_notice: str | None = Field(default=None, max_length=1000)
    call_disclosure_enabled: bool = True
    call_disclosure_text: str = Field(min_length=10, max_length=300)
    request_sla_days: int = Field(default=30, ge=1, le=90)

    @field_validator("purposes")
    @classmethod
    def _purposes(cls, value: list[str]) -> list[str]:
        cleaned = [p.strip()[:160] for p in value if p and p.strip()]
        if not cleaned:
            raise ValueError("List at least one reason you collect personal data.")
        return cleaned


class ChecklistItem(BaseModel):
    key: str
    label: str
    ok: bool
    hint: str
    href: str


class Checklist(BaseModel):
    items: list[ChecklistItem]
    score: int
    total: int


class ConsentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    subject_name: str | None
    subject_phone: str | None
    subject_email: str | None
    purpose: str
    status: str
    source: str
    notice_version: int | None
    granted_at: datetime
    withdrawn_at: datetime | None
    withdrawn_via: str | None


class WithdrawBody(BaseModel):
    phone: str | None = Field(default=None, max_length=32)
    email: str | None = Field(default=None, max_length=254)
    purposes: list[str] | None = None

    @field_validator("purposes")
    @classmethod
    def _purposes(cls, value):
        if value is not None and any(p not in CONSENT_PURPOSES for p in value):
            raise ValueError("Unknown purpose")
        return value


class RequestCreate(BaseModel):
    kind: str
    name: str = Field(min_length=1, max_length=255)
    email: str | None = Field(default=None, max_length=254)
    phone: str | None = Field(default=None, max_length=32)
    details: str | None = Field(default=None, max_length=4000)

    @field_validator("kind")
    @classmethod
    def _kind(cls, value: str) -> str:
        if value not in REQUEST_KINDS:
            raise ValueError("Unknown request type")
        return value


class RequestRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    reference: str
    kind: str
    status: str
    subject_name: str
    subject_email: str | None
    subject_phone: str | None
    details: str | None
    customer_id: uuid.UUID | None
    verified_at: datetime | None
    due_at: datetime
    completed_at: datetime | None
    resolution: str | None
    created_at: datetime
    state: str = "open"


class RequestResolve(BaseModel):
    resolution: str = Field(min_length=3, max_length=2000)


class BreachCreate(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    description: str = Field(min_length=10, max_length=5000)
    discovered_at: datetime | None = None
    occurred_at: datetime | None = None
    data_categories: list[str] = Field(default_factory=list, max_length=20)
    approx_people: int | None = Field(default=None, ge=0)
    likely_impact: str | None = Field(default=None, max_length=3000)
    mitigation: str | None = Field(default=None, max_length=3000)


class BreachUpdate(BaseModel):
    likely_impact: str | None = Field(default=None, max_length=3000)
    mitigation: str | None = Field(default=None, max_length=3000)
    approx_people: int | None = Field(default=None, ge=0)
    board_notified: bool | None = None
    board_reference: str | None = Field(default=None, max_length=120)
    principals_notified: bool | None = None
    principals_notified_how: str | None = Field(default=None, max_length=2000)
    closed: bool | None = None


class BreachRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    description: str
    discovered_at: datetime
    occurred_at: datetime | None
    data_categories: list[str]
    approx_people: int | None
    likely_impact: str | None
    mitigation: str | None
    status: str
    report_due_at: datetime
    board_notified_at: datetime | None
    board_reference: str | None
    principals_notified_at: datetime | None
    principals_notified_how: str | None
    closed_at: datetime | None
    state: str = "report_due"


class PublicRequestBody(RequestCreate):
    website: str | None = None  # bot trap


class PublicStatusBody(BaseModel):
    reference: str = Field(max_length=16)
    contact: str = Field(max_length=254)
