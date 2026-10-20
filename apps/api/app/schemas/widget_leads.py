import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class WidgetLeadRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str | None
    phone: str | None
    email: str | None
    interest: str | None
    page_url: str | None
    consent_call: bool
    call_status: str
    call_attempts: int
    call_id: uuid.UUID | None
    crm_lead_id: uuid.UUID | None
    conversation_id: uuid.UUID | None
    created_at: datetime


class WidgetLeadDetail(WidgetLeadRead):
    referrer: str | None
    utm: dict
    user_agent: str | None
    ip_address: str | None
    consent_text: str | None
    consent_at: datetime | None
    consent_ip: str | None
    call_note: str | None
    call_requested_at: datetime | None
    last_call_error: str | None
    customer_id: uuid.UUID | None


class WidgetLeadSummary(BaseModel):
    days: int
    leads: int
    with_phone: int
    call_requests: int
    calls_completed: int
    needs_follow_up: int
