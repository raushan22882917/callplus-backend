import enum
import uuid

from sqlalchemy import Enum, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base
from packages.database.mixins import TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin


class IntegrationStatus(str, enum.Enum):
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    ERROR = "error"


class Integration(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """One row per (tenant, provider) — non-secret config only. Secrets live
    in IntegrationCredential, encrypted. `provider` is a free-string, not an
    enum, so adding a new integration type never needs a migration."""

    __tablename__ = "integrations"

    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[IntegrationStatus] = mapped_column(
        Enum(IntegrationStatus, name="integration_status"),
        default=IntegrationStatus.DISCONNECTED,
        nullable=False,
    )
    config: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    __table_args__ = (
        UniqueConstraint("tenant_id", "provider", name="uq_integration_tenant_provider"),
    )


class IntegrationCredential(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    __tablename__ = "integration_credentials"

    integration_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("integrations.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    encrypted_secret: Mapped[str] = mapped_column(String(4000), nullable=False)
