import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---- security -------------------------------------------------------------


class TwoFactorStatus(BaseModel):
    enabled: bool
    pending_enrollment: bool
    recovery_codes_remaining: int


class TwoFactorEnrollResponse(BaseModel):
    secret: str
    otpauth_uri: str


class TwoFactorCodeRequest(BaseModel):
    code: str = Field(min_length=6, max_length=32)


class TwoFactorVerifyResponse(BaseModel):
    enabled: bool
    recovery_codes: list[str]


class TwoFactorDisableRequest(BaseModel):
    password: str
    code: str = Field(min_length=6, max_length=32)


class SecuritySettingsRead(BaseModel):
    ip_allowlist: list[str]
    mask_pii: bool
    mask_phone_non_admin: bool
    your_ip: str | None


class SecuritySettingsUpdate(BaseModel):
    ip_allowlist: list[str] | None = None
    mask_pii: bool | None = None
    mask_phone_non_admin: bool | None = None


class RetentionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    enabled: bool
    transcript_days: int | None
    recording_days: int | None
    last_purged_at: datetime | None
    last_purge_summary: dict[str, Any]


class RetentionUpdate(BaseModel):
    enabled: bool
    transcript_days: int | None = Field(default=None, ge=1, le=3650)
    recording_days: int | None = Field(default=None, ge=1, le=3650)


class RetentionPurgeResult(BaseModel):
    transcripts_deleted: int
    recordings_cleared: int


class DataExportRequest(BaseModel):
    customer_id: uuid.UUID


class ErasureResult(BaseModel):
    customer_id: uuid.UUID
    deleted: dict[str, int]


# ---- SSO ------------------------------------------------------------------


class SsoConfigRead(BaseModel):
    enabled: bool
    issuer: str
    client_id: str
    has_client_secret: bool
    default_role_name: str
    allowed_email_domains: list[str]
    login_url: str
    redirect_uri: str


class SsoConfigWrite(BaseModel):
    enabled: bool = True
    issuer: str = Field(min_length=8, max_length=512)
    client_id: str = Field(min_length=1, max_length=255)
    # Omit on update to keep the stored secret.
    client_secret: str | None = Field(default=None, max_length=1024)
    default_role_name: str = "AGENT"
    allowed_email_domains: list[str] = []

    @field_validator("default_role_name")
    @classmethod
    def _not_owner(cls, v: str) -> str:
        if v.upper() == "OWNER":
            raise ValueError("SSO users cannot be auto-provisioned as OWNER")
        return v.upper()


# ---- usage / pricing -------------------------------------------------------

METRICS = ("ai_minutes", "human_minutes", "whatsapp_messages", "storage_bytes", "sms")


class UsageLine(BaseModel):
    metric: str
    label: str
    unit: str
    quantity: float
    rate: float
    amount: float
    limit: float | None
    over_limit: bool


class UsageSummary(BaseModel):
    month: str
    currency: str
    plan_id: uuid.UUID | None
    plan_name: str | None
    base_fee: float
    lines: list[UsageLine]
    estimated_total: float
    rates_source: str  # "pricing_plan" | "defaults"


class UsageEventCreate(BaseModel):
    metric: str
    quantity: float
    occurred_at: datetime | None = None
    reference_id: uuid.UUID | None = None
    extra: dict[str, Any] = {}

    @field_validator("metric")
    @classmethod
    def _known_metric(cls, v: str) -> str:
        if v not in METRICS:
            raise ValueError(f"metric must be one of {', '.join(METRICS)}")
        return v


class UsageEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    metric: str
    quantity: float
    occurred_at: datetime
    reference_id: uuid.UUID | None
    extra: dict[str, Any]


class _PricingBase(BaseModel):
    currency: str = Field(default="INR", min_length=3, max_length=3)
    rates: dict[str, float] = {}
    limits: dict[str, float] = {}
    base_fee: float = Field(default=0, ge=0)

    @field_validator("rates", "limits")
    @classmethod
    def _metrics_known(cls, v: dict[str, float]) -> dict[str, float]:
        for key, value in v.items():
            if key not in METRICS:
                raise ValueError(f"unknown metric {key!r}")
            if value < 0:
                raise ValueError("values must be >= 0")
        return v


class PricingPlanCreate(_PricingBase):
    name: str = Field(min_length=1, max_length=100)
    is_active: bool = False


class PricingPlanUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    rates: dict[str, float] | None = None
    limits: dict[str, float] | None = None
    base_fee: float | None = Field(default=None, ge=0)
    is_active: bool | None = None


class PricingPlanRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    currency: str
    is_active: bool
    rates: dict[str, float]
    limits: dict[str, float]
    base_fee: float
    created_at: datetime


class LimitCheck(BaseModel):
    metric: str
    used: float
    limit: float | None
    remaining: float | None
    exceeded: bool


# ---- admin -----------------------------------------------------------------


class AdminTenantRow(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    status: str
    plan_tier: str = "legacy"
    created_at: datetime
    users: int
    phone_numbers: int
    calls_month: int
    usage_cost_month: float
    currency: str


class AdminTenantDetail(AdminTenantRow):
    user_list: list[dict[str, Any]]
    number_list: list[dict[str, Any]]
    usage: UsageSummary


class AdminHealth(BaseModel):
    version: str
    environment: str
    database: dict[str, Any]
    redis: dict[str, Any]
    celery: dict[str, Any]


class AdminStatusChange(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class AdminPlanChange(BaseModel):
    """Sets a workspace's plan tier and any extra grants on top of it, e.g.
    {"tier": "free", "overrides": {"bonus_minutes": 20}}. `overrides` replaces the
    previous overrides entirely; send {} to clear them."""

    tier: str = Field(min_length=1, max_length=20)
    overrides: dict[str, Any] | None = None
    reason: str | None = Field(default=None, max_length=500)


# ---- connectors ------------------------------------------------------------


class ConnectorFieldInfo(BaseModel):
    key: str
    label: str
    secret: bool = False
    required: bool = True
    placeholder: str | None = None


class ConnectorCatalogEntry(BaseModel):
    type: str
    label: str
    category: str
    description: str
    supported: bool
    unsupported_reason: str | None
    oauth_provider: str | None = None
    fields: list[ConnectorFieldInfo]
    default_events: list[str]


class ConnectorCreate(BaseModel):
    type: str
    name: str = Field(min_length=1, max_length=120)
    config: dict[str, str]
    enabled: bool = True
    events: list[str] = []


class ConnectorUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    # Only keys present are changed; blank secret values keep the stored one.
    config: dict[str, str] | None = None
    enabled: bool | None = None
    events: list[str] | None = None


class ConnectorRead(BaseModel):
    id: uuid.UUID
    type: str
    name: str
    enabled: bool
    events: list[str]
    # Secrets are never returned; they appear as "********" placeholders.
    config: dict[str, str]
    last_test_at: datetime | None
    last_test_ok: bool | None
    last_error: str | None
    created_at: datetime


class ConnectorTestResult(BaseModel):
    ok: bool
    detail: str


class SdkInfo(BaseModel):
    openapi_url: str
    api_base_url: str
    auth: str
    webhook_signature: dict[str, str]
    sdks: list[dict[str, str]]
