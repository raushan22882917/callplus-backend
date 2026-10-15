import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

ApiKeyScope = Literal[
    "calls:write",
    "calls:read",
    "agents:read",
    "agents:write",
    "customers:read",
    "customers:write",
    "messages:write",
    "numbers:read",
    "numbers:write",
    "campaigns:read",
    "campaigns:write",
    "webhooks:manage",
]

WebhookEventName = Literal[
    "call.started",
    "call.ended",
    "call.transcript_ready",
    "ticket.created",
    "message.received",
    "campaign.completed",
]


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    mode: Literal["live", "test"] = "test"
    scopes: list[ApiKeyScope] = Field(min_length=1)
    expires_at: datetime | None = None


class ApiKeyRead(BaseModel):
    id: uuid.UUID
    name: str
    prefix: str
    mode: Literal["live", "test"]
    scopes: list[str]
    last_used_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime


class ApiKeyCreated(ApiKeyRead):
    """`secret` is returned exactly once, here; only its hash is stored."""

    secret: str


class WebhookEndpointCreate(BaseModel):
    url: str = Field(max_length=2048)
    events: list[WebhookEventName] = Field(min_length=1)

    @field_validator("events")
    @classmethod
    def _unique(cls, v: list[str]) -> list[str]:
        return sorted(set(v))


class WebhookEndpointUpdate(BaseModel):
    url: str | None = Field(default=None, max_length=2048)
    events: list[WebhookEventName] | None = Field(default=None, min_length=1)
    enabled: bool | None = None


class WebhookEndpointRead(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    url: str
    events: list[str]
    enabled: bool
    created_at: datetime


class WebhookEndpointCreated(WebhookEndpointRead):
    signing_secret: str


class WebhookDeliveryRead(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    endpoint_id: uuid.UUID
    event: str
    status: Literal["pending", "succeeded", "failed"]
    response_code: int | None
    attempts: int
    created_at: datetime


class UsageQuota(BaseModel):
    call_minutes: int | None
    api_requests_per_minute: int | None
    concurrent_calls: int | None


class UsageDay(BaseModel):
    day: date
    call_minutes: float
    api_requests: int


class UsageSummary(BaseModel):
    period_start: datetime
    period_end: datetime
    call_minutes: float
    stt_minutes: float
    tts_characters: int
    whatsapp_messages: int
    api_requests: int
    estimated_cost_paise: int
    quota: UsageQuota
    daily: list[UsageDay]
