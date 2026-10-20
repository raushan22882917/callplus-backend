import re
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class PostCallSettingsRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    enabled: bool
    customer_sms: bool
    customer_email: bool
    staff_email: bool
    staff_emails: list[str]
    include_summary: bool
    min_call_seconds: int
    sms_message: str
    email_subject: str


class PostCallSettingsUpdate(BaseModel):
    enabled: bool | None = None
    customer_sms: bool | None = None
    customer_email: bool | None = None
    staff_email: bool | None = None
    staff_emails: list[str] | None = None
    include_summary: bool | None = None
    min_call_seconds: int | None = Field(default=None, ge=0, le=3600)
    sms_message: str | None = Field(default=None, min_length=5, max_length=320)
    email_subject: str | None = Field(default=None, min_length=3, max_length=150)

    @field_validator("staff_emails")
    @classmethod
    def _emails(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return value
        cleaned = sorted({e.strip().lower() for e in value if e.strip()})
        if len(cleaned) > 5:
            raise ValueError("use at most 5 team emails")
        bad = [e for e in cleaned if len(e) > 254 or not _EMAIL.match(e)]
        if bad:
            raise ValueError(f"not an email address: {bad[0]}")
        return cleaned


class CallNotificationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    call_id: uuid.UUID
    channel: str
    audience: str
    recipient: str
    status: str
    error: str | None
    created_at: datetime
