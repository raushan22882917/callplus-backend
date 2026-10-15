import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base
from packages.database.mixins import TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin


class McpApiKey(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """A credential an external MCP client (ChatGPT, Claude, or any other
    MCP host) presents as `Authorization: Bearer <key>` to app/mcp/server.py
    to act on behalf of this tenant — the only auth scheme those clients
    use, since they have no human login session. Only the SHA-256 hash is
    stored; the plaintext key is shown once, at creation (see
    app/api/v1/mcp.py), and can never be recovered afterwards.
    """

    __tablename__ = "mcp_api_keys"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # First few characters of the plaintext key, kept so the owner can tell
    # keys apart in a list without the platform ever storing the full value.
    key_prefix: Mapped[str] = mapped_column(String(20), nullable=False)
    hashed_key: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
