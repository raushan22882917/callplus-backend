import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import expression

from packages.database.base import Base
from packages.database.mixins import TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin

CALL_STATUSES = ("none", "requested", "calling", "completed", "no_answer", "failed", "blocked")


class WidgetLead(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """Everything captured from one website chat visit: who they are, where they
    came from, whether they agreed to be called, and how the call went. The CRM
    lead holds the sales view; this keeps the evidence (consent, attribution)
    that does not belong on it. One row per chat session."""

    __tablename__ = "widget_leads"

    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="SET NULL"),
        unique=True,
    )
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id", ondelete="SET NULL"), index=True
    )
    crm_lead_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("crm_leads.id", ondelete="SET NULL"), index=True
    )

    name: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(32))
    email: Mapped[str | None] = mapped_column(String(255))
    interest: Mapped[str | None] = mapped_column(Text)
    # Everything the visitor filled in on the business's own form, as the business defined it:
    # [{"key", "label", "type", "value"}]. The labels are kept as they were when the visitor answered.
    answers: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]", nullable=False)

    # Where the visitor came from.
    page_url: Mapped[str | None] = mapped_column(String(2048))
    referrer: Mapped[str | None] = mapped_column(String(2048))
    utm: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    user_agent: Mapped[str | None] = mapped_column(String(255))
    ip_address: Mapped[str | None] = mapped_column(String(45))

    # Proof that they agreed to a call, and to which wording.
    consent_call: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=expression.false(), nullable=False
    )
    consent_text: Mapped[str | None] = mapped_column(String(600))
    consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consent_ip: Mapped[str | None] = mapped_column(String(45))

    call_status: Mapped[str] = mapped_column(
        String(12), default="none", server_default="none", nullable=False
    )
    call_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calls.id", ondelete="SET NULL"), index=True
    )
    call_attempts: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    call_note: Mapped[str | None] = mapped_column(String(500))
    call_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_call_error: Mapped[str | None] = mapped_column(String(300))

    __table_args__ = (
        Index("ix_widget_leads_tenant_created", "tenant_id", "created_at"),
        Index("ix_widget_leads_tenant_call_status", "tenant_id", "call_status"),
        Index("ix_widget_leads_tenant_phone", "tenant_id", "phone"),
    )
