import uuid
from datetime import date, datetime
from typing import Literal

from app.core.ip_allowlist import normalize_entry
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


def _clean_ips(value: list[str] | None) -> list[str] | None:
    if value is None:
        return value
    if len(value) > 20:
        raise ValueError("use at most 20 IP addresses or ranges")
    try:
        return sorted({normalize_entry(entry) for entry in value if entry.strip()})
    except ValueError as exc:
        raise ValueError(f"not a valid IP address or range: {exc}") from exc


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    mode: Literal["live", "test"] = "test"
    scopes: list[ApiKeyScope] = Field(min_length=1)
    expires_at: datetime | None = None
    allowed_ips: list[str] = Field(default_factory=list)

    _ips = field_validator("allowed_ips")(_clean_ips)


class ApiKeyUpdate(BaseModel):
    """Edit a key without changing its secret. Omitted fields are unchanged."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    allowed_ips: list[str] | None = None
    expires_at: datetime | None = None

    _ips = field_validator("allowed_ips")(_clean_ips)


class ApiKeyRotate(BaseModel):
    # How long the old key keeps working so deployments can switch over. 0 = revoke now.
    grace_hours: int = Field(default=24, ge=0, le=168)


class ApiKeyRead(BaseModel):
    id: uuid.UUID
    name: str
    prefix: str
    mode: Literal["live", "test"]
    scopes: list[str]
    allowed_ips: list[str] = Field(default_factory=list)
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
