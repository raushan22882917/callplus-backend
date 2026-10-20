import uuid
from datetime import date, datetime

from sqlalchemy import (
    ARRAY,
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base
from packages.database.mixins import TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin


class ApiKey(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """A credential a tenant's own software presents as `Authorization:
    Bearer ak_live_…` / `ak_test_…` to call the public API (/calls,
    /ai-agents, /customers …) without a human login session. Only the SHA-256
    hash is stored; the plaintext is shown once at creation. `scopes` decide
    which permissions the key carries (see app/core/api_keys.py). Test-mode
    keys never dial or bill. Distinct from McpApiKey, which only authenticates
    external MCP hosts."""

    __tablename__ = "api_keys"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    key_prefix: Mapped[str] = mapped_column(String(24), nullable=False)
    hashed_key: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    mode: Mapped[str] = mapped_column(String(8), nullable=False, default="test")
    scopes: Mapped[list[str]] = mapped_column(ARRAY(String(32)), nullable=False, default=list)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # IPs or CIDR blocks this key may be used from; empty = anywhere (still
    # subject to the workspace allowlist). A leaked key is useless off-network.
    allowed_ips: Mapped[list] = mapped_column(
        JSONB, default=list, server_default="[]", nullable=False
    )


class ApiKeyUsageDaily(Base, UUIDPrimaryKeyMixin, TenantScopedMixin):
    """Requests made with API keys, per key per UTC day — feeds the Usage tab."""

    __tablename__ = "api_key_usage_daily"

    api_key_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("api_keys.id", ondelete="CASCADE"), nullable=False
    )
    day: Mapped[date] = mapped_column(Date, nullable=False)
    requests: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (UniqueConstraint("api_key_id", "day", name="uq_api_key_usage_key_day"),)


class WebhookEndpoint(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    __tablename__ = "webhook_endpoints"

    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    events: Mapped[list[str]] = mapped_column(ARRAY(String(48)), nullable=False, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Fernet-encrypted: the platform must be able to recompute the HMAC for
    # every delivery, so unlike API keys this cannot be a one-way hash.
    signing_secret_encrypted: Mapped[str] = mapped_column(Text, nullable=False)


class WebhookDelivery(Base, UUIDPrimaryKeyMixin, TenantScopedMixin):
    __tablename__ = "webhook_deliveries"

    endpoint_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("webhook_endpoints.id", ondelete="CASCADE"), nullable=False
    )
    event: Mapped[str] = mapped_column(String(48), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="pending")
    response_code: Mapped[int | None] = mapped_column(Integer)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_webhook_deliveries_tenant_created", "tenant_id", "created_at"),)


class IdempotencyRecord(Base, UUIDPrimaryKeyMixin, TenantScopedMixin):
    """Remembers which resource an `Idempotency-Key` produced, so a retried
    POST /calls returns the original call instead of dialling twice."""

    __tablename__ = "idempotency_records"

    key: Mapped[str] = mapped_column(String(128), nullable=False)
    call_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calls.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (UniqueConstraint("tenant_id", "key", name="uq_idempotency_tenant_key"),)
