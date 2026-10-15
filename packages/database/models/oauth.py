"""Per-tenant OAuth: each business authorizes the platform's OAuth app and its
tokens live here, Fernet-encrypted (app/core/crypto.py). The platform's own
client id/secret per provider stay in .env."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base
from packages.database.mixins import TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin


class OAuthConnectionStatus(str, enum.Enum):
    CONNECTED = "connected"
    EXPIRED = "expired"
    REVOKED = "revoked"
    ERROR = "error"


class OAuthConnection(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """One row per (tenant, provider). `status` is a plain string so adding a
    state never needs a type migration."""

    __tablename__ = "oauth_connections"

    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    # Human label shown in the UI ("Acme Workspace", a shop domain, an email).
    account_label: Mapped[str | None] = mapped_column(String(255))
    account_email: Mapped[str | None] = mapped_column(String(255))
    external_account_id: Mapped[str | None] = mapped_column(String(255))
    scopes: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    encrypted_access_token: Mapped[str] = mapped_column(Text, nullable=False)
    encrypted_refresh_token: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # connected | expired | revoked | error
    status: Mapped[str] = mapped_column(
        String(16), default=OAuthConnectionStatus.CONNECTED.value, nullable=False
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    connected_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    # Provider-specific, non-secret data: instance_url, api_domain, selected
    # calendar/channel/page, ... Column is named "metadata" (reserved attribute name).
    extra: Mapped[dict] = mapped_column("metadata", JSONB, default=dict, nullable=False)

    __table_args__ = (
        UniqueConstraint("tenant_id", "provider", name="uq_oauth_connection_tenant_provider"),
    )


class OAuthState(Base):
    """Server-side half of an in-flight authorization: single-use nonce plus
    the PKCE code_verifier (encrypted). Deliberately has no tenant_id column:
    the public callback has no tenant context. The signed `state` parameter
    carries tenant/user; this row makes it single use and holds the verifier."""

    __tablename__ = "oauth_states"

    nonce: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    # connect | login
    purpose: Mapped[str] = mapped_column(String(16), nullable=False)
    encrypted_code_verifier: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_oauth_states_expires_at", "expires_at"),)
