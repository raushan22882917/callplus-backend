"""Operations module tables: SLA tracking, appointments, follow-up automation
and collections. Every table is tenant-scoped. Status/priority-like columns
are plain strings validated in app/schemas/operations.py (no Postgres enum
types, so adding a state later never needs an ALTER TYPE)."""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
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


class TicketCategory(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """Ticket category / owning department (SLA summaries group by department)."""

    __tablename__ = "ticket_categories"

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    department: Mapped[str] = mapped_column(String(120), nullable=False, default="General")
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_ticket_category_name"),)


class SLAPolicy(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    __tablename__ = "sla_policies"

    priority: Mapped[str] = mapped_column(String(16), nullable=False)
    first_response_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    resolution_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    business_hours_only: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    business_start_hour: Mapped[int] = mapped_column(Integer, default=9, nullable=False)
    business_end_hour: Mapped[int] = mapped_column(Integer, default=18, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), default="UTC", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    __table_args__ = (UniqueConstraint("tenant_id", "priority", name="uq_sla_policy_priority"),)


class TicketSLA(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    __tablename__ = "ticket_sla"

    ticket_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tickets.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    policy_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sla_policies.id", ondelete="SET NULL")
    )
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ticket_categories.id", ondelete="SET NULL")
    )
    department: Mapped[str | None] = mapped_column(String(120))
    priority: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    first_response_due_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    resolution_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    first_responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_response_breached: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    resolution_breached: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    breach_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_ticket_sla_tenant_open", "tenant_id", "resolved_at"),)


class BookableService(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    __tablename__ = "services"

    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_service_name"),)


class AppointmentResource(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """A bookable staff member, chair or room. working_hours maps a weekday
    key (mon..sun) to a list of ["HH:MM", "HH:MM"] windows in `timezone`."""

    __tablename__ = "appointment_resources"

    name: Mapped[str] = mapped_column(String(160), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), default="staff", nullable=False)
    working_hours: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), default="UTC", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Appointment(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    __tablename__ = "appointments"

    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("customers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    service_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("services.id", ondelete="RESTRICT"), nullable=False
    )
    resource_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("appointment_resources.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="booked", nullable=False)
    source: Mapped[str] = mapped_column(String(16), default="manual", nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    reminder_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reminder_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reminder_error: Mapped[str | None] = mapped_column(Text)
    no_show_followup_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # {"google": "<event id>", "microsoft": "<event id>"}: the mirrored external
    # calendar events (app/services/calendar_sync.py).
    calendar_events: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default="{}", nullable=False
    )

    __table_args__ = (
        Index("ix_appointments_tenant_start", "tenant_id", "start_at"),
        Index("ix_appointments_resource_start", "resource_id", "start_at"),
    )


class FollowUp(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    __tablename__ = "follow_ups"

    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("customers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    created_by: Mapped[str] = mapped_column(String(16), default="human", nullable=False)
    reference_type: Mapped[str | None] = mapped_column(String(32))
    reference_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_follow_ups_status_due", "status", "due_at"),)


class PaymentPromise(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    __tablename__ = "payment_promises"

    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("customers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="INR", nullable=False)
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    promised_on: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    call_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_by: Mapped[str] = mapped_column(String(16), default="human", nullable=False)

    __table_args__ = (Index("ix_payment_promises_tenant_status_due", "tenant_id", "status", "due_date"),)
