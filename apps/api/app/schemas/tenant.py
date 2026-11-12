import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from packages.database.models import TenantStatus


class TenantRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    industry: str | None
    timezone: str
    country: str | None
    currency: str
    status: TenantStatus
    spam_protection_enabled: bool
    spam_call_window_minutes: int
    spam_call_threshold: int
    call_recording_enabled: bool
    # When the business told its phone company which numbers place automated calls.
    # Voice campaigns cannot start until this is set.
    a2p_declared_at: datetime | None = None
    created_at: datetime


class TenantUpdate(BaseModel):
    name: str | None = None
    industry: str | None = None
    timezone: str | None = None
    country: str | None = None
    currency: str | None = None
    spam_protection_enabled: bool | None = None
    spam_call_window_minutes: int | None = Field(default=None, ge=1, le=1440)
    spam_call_threshold: int | None = Field(default=None, ge=1, le=1000)
    call_recording_enabled: bool | None = None
