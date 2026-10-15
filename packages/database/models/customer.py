import uuid
from datetime import datetime

from sqlalchemy import ARRAY, DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base
from packages.database.mixins import TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin


class Customer(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    __tablename__ = "customers"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(32))
    email: Mapped[str | None] = mapped_column(String(255))
    tags: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, nullable=False)
    extra: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    __table_args__ = (
        Index("ix_customers_tenant_phone", "tenant_id", "phone"),
        Index("ix_customers_tenant_email", "tenant_id", "email"),
    )


class CustomerActivity(Base, UUIDPrimaryKeyMixin, TenantScopedMixin):
    """Unified timeline entry for a customer — one row per call, message,
    ticket, note, etc. `reference_id` points at the source record."""

    __tablename__ = "customer_activities"

    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("customers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    type: Mapped[str] = mapped_column(String(50), nullable=False)
    source: Mapped[str | None] = mapped_column(String(50))
    reference_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    extra: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index(
            "ix_customer_activities_tenant_customer_created",
            "tenant_id",
            "customer_id",
            "created_at",
        ),
    )
