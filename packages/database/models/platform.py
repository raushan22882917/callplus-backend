"""Platform module tables: security settings, 2FA, SSO, retention, usage
metering, pricing, super-admin registry and outbound connectors.

`user_totp` and `platform_admins` are keyed by user (users are global, not
tenant-owned); everything else is tenant-scoped.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base
from packages.database.mixins import TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin


class UserTotp(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """TOTP 2FA enrollment for a user. `enabled` only flips to true after the
    user proves they hold the secret by submitting a valid code."""

    __tablename__ = "user_totp"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    encrypted_secret: Mapped[str] = mapped_column(String(512), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # sha256 hex digests of the unused single-use recovery codes.
    recovery_code_hashes: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    # Last accepted 30s time step; a code for a step <= this is a replay.
    last_used_step: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class PlatformAdmin(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Registry of super-admin users (alternative to altering `users`)."""

    __tablename__ = "platform_admins"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )


class TenantSecuritySettings(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    __tablename__ = "tenant_security_settings"

    # CIDR blocks or single IPs. Empty list = no restriction.
    ip_allowlist: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    # Mask phone/email/Aadhaar/card numbers inside transcripts and summaries.
    mask_pii: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Mask phone numbers for roles other than OWNER/ADMIN.
    mask_phone_non_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    __table_args__ = (UniqueConstraint("tenant_id", name="uq_tenant_security_settings_tenant"),)


class RetentionPolicy(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    __tablename__ = "retention_policies"

    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    transcript_days: Mapped[int | None] = mapped_column(Integer)
    recording_days: Mapped[int | None] = mapped_column(Integer)
    last_purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_purge_summary: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    __table_args__ = (UniqueConstraint("tenant_id", name="uq_retention_policy_tenant"),)


class SsoConfig(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """Per-tenant OIDC (authorization-code) configuration."""

    __tablename__ = "sso_configs"

    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    issuer: Mapped[str] = mapped_column(String(512), nullable=False)
    client_id: Mapped[str] = mapped_column(String(255), nullable=False)
    encrypted_client_secret: Mapped[str] = mapped_column(String(1024), nullable=False)
    # New SSO users are created with this system role (never OWNER).
    default_role_name: Mapped[str] = mapped_column(String(50), default="AGENT", nullable=False)
    allowed_email_domains: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)

    __table_args__ = (UniqueConstraint("tenant_id", name="uq_sso_config_tenant"),)


class UsageEvent(Base, UUIDPrimaryKeyMixin, TenantScopedMixin):
    """Metering ledger. AI/human minutes and WhatsApp messages are derived
    from calls/messages; this table carries what has no other source of truth
    (SMS, storage bytes) and manual adjustments for any metric."""

    __tablename__ = "usage_events"

    metric: Mapped[str] = mapped_column(String(32), nullable=False)
    quantity: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reference_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    extra: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    __table_args__ = (
        Index("ix_usage_events_tenant_metric_time", "tenant_id", "metric", "occurred_at"),
    )


class PricingPlan(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """Per-unit rates and monthly limits used for the itemized estimate.
    `rates`/`limits` map metric -> number (rates in `currency` per unit)."""

    __tablename__ = "pricing_plans"

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="INR", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    rates: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    limits: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    base_fee: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)


class IntegrationConnector(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """Outbound connector to a third-party system. `config` is
    {"enc": "<fernet ciphertext of the JSON config>"} — never stored in the
    clear because it holds API tokens/webhook URLs."""

    __tablename__ = "integration_connectors"

    type: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    config: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    # Events (e.g. call.ended) this connector is notified of.
    events: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    last_test_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_test_ok: Mapped[bool | None] = mapped_column(Boolean)
    last_error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_integration_connectors_tenant_type", "tenant_id", "type"),)
