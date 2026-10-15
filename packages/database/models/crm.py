import enum
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    ARRAY,
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base
from packages.database.mixins import TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin


class CrmStage(str, enum.Enum):
    NEW_LEAD = "new_lead"
    CONTACTED = "contacted"
    QUALIFIED = "qualified"
    DEMO = "demo"
    PROPOSAL = "proposal"
    NEGOTIATION = "negotiation"
    WON = "won"
    LOST = "lost"


# Board column order, left to right.
CRM_STAGE_ORDER: list[CrmStage] = list(CrmStage)


class CrmTaskType(str, enum.Enum):
    CALL = "call"
    WHATSAPP = "whatsapp"
    EMAIL = "email"
    TODO = "todo"


class CrmTaskStatus(str, enum.Enum):
    OPEN = "open"
    DONE = "done"
    CANCELLED = "cancelled"


class CrmCompany(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    __tablename__ = "crm_companies"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    domain: Mapped[str | None] = mapped_column(String(255))
    industry: Mapped[str | None] = mapped_column(String(120))
    phone: Mapped[str | None] = mapped_column(String(32))
    website: Mapped[str | None] = mapped_column(String(255))
    notes: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_crm_companies_tenant_name", "tenant_id", "name"),)


class CrmLead(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    __tablename__ = "crm_leads"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(32))
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("crm_companies.id", ondelete="SET NULL"), index=True
    )
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id", ondelete="SET NULL"), index=True
    )
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    source_call_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calls.id", ondelete="SET NULL")
    )
    stage: Mapped[CrmStage] = mapped_column(
        Enum(CrmStage, name="crm_stage"), default=CrmStage.NEW_LEAD, nullable=False
    )
    stage_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    lost_reason: Mapped[str | None] = mapped_column(String(255))
    source: Mapped[str | None] = mapped_column(String(80))
    tags: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, nullable=False)
    custom_fields: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    value: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="INR", nullable=False)
    score: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    score_breakdown: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    scored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_crm_leads_tenant_stage", "tenant_id", "stage"),
        Index("ix_crm_leads_tenant_phone", "tenant_id", "phone"),
        Index("ix_crm_leads_tenant_email", "tenant_id", "email"),
    )


class CrmDeal(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    __tablename__ = "crm_deals"

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("crm_leads.id", ondelete="SET NULL"), index=True
    )
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("crm_companies.id", ondelete="SET NULL")
    )
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id", ondelete="SET NULL")
    )
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    stage: Mapped[CrmStage] = mapped_column(
        Enum(CrmStage, name="crm_stage"), default=CrmStage.QUALIFIED, nullable=False
    )
    value: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="INR", nullable=False)
    expected_close_date: Mapped[date | None] = mapped_column(Date)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_crm_deals_tenant_stage", "tenant_id", "stage"),)


class CrmTask(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    __tablename__ = "crm_tasks"

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    type: Mapped[CrmTaskType] = mapped_column(
        Enum(CrmTaskType, name="crm_task_type"), default=CrmTaskType.TODO, nullable=False
    )
    status: Mapped[CrmTaskStatus] = mapped_column(
        Enum(CrmTaskStatus, name="crm_task_status"), default=CrmTaskStatus.OPEN, nullable=False
    )
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    assignee_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("crm_leads.id", ondelete="CASCADE"), index=True
    )
    deal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("crm_deals.id", ondelete="CASCADE"), index=True
    )
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id", ondelete="SET NULL")
    )
    created_by_ai: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    __table_args__ = (Index("ix_crm_tasks_tenant_status_due", "tenant_id", "status", "due_at"),)


class CrmNote(Base, UUIDPrimaryKeyMixin, TenantScopedMixin):
    """Free-text note, or a system timeline entry (kind != "note", e.g. a
    stage change) attached to a lead and/or deal."""

    __tablename__ = "crm_notes"

    kind: Mapped[str] = mapped_column(String(24), default="note", nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    author_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("crm_leads.id", ondelete="CASCADE"), index=True
    )
    deal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("crm_deals.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
