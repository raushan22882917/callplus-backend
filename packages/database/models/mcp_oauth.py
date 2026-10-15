import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base
from packages.database.mixins import TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin


class McpOAuthClient(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """An AI host registered through RFC 7591 dynamic client registration.
    Public clients only (PKCE, no client secret), so it carries no tenant:
    which tenant it acts for is decided per authorization by the user who
    approves it."""

    __tablename__ = "mcp_oauth_clients"

    client_name: Mapped[str] = mapped_column(String(200), nullable=False)
    redirect_uris: Mapped[list[str]] = mapped_column(ARRAY(String(2048)), nullable=False)


class McpOAuthCode(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """Single-use authorization code (hash only), bound to PKCE challenge,
    redirect URI and resource."""

    __tablename__ = "mcp_oauth_codes"

    hashed_code: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    client_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("mcp_oauth_clients.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    scopes: Mapped[list[str]] = mapped_column(ARRAY(String(32)), nullable=False)
    redirect_uri: Mapped[str] = mapped_column(String(2048), nullable=False)
    code_challenge: Mapped[str] = mapped_column(String(128), nullable=False)
    resource: Mapped[str | None] = mapped_column(String(2048))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class McpOAuthToken(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """An access/refresh token pair (hashes only) issued to a client on behalf
    of a user in one tenant."""

    __tablename__ = "mcp_oauth_tokens"

    hashed_access_token: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False
    )
    hashed_refresh_token: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False
    )
    client_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("mcp_oauth_clients.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    scopes: Mapped[list[str]] = mapped_column(ARRAY(String(32)), nullable=False)
    resource: Mapped[str | None] = mapped_column(String(2048))
    access_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    refresh_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set when the pair is rotated or revoked. Presenting a rotated refresh
    # token again is treated as theft and revokes the whole client+user family.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
