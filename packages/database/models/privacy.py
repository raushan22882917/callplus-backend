import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import expression

from packages.database.base import Base
from packages.database.mixins import TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin

DEFAULT_PURPOSES = [
    "Replying to your enquiry",
    "Calling or messaging you about it, if you asked us to",
    "Keeping a record of our conversation so we can help you again",
]
DEFAULT_RETENTION = "We keep your details only as long as we need them to deal with your enquiry or to meet a legal duty, then delete them."
DEFAULT_CALL_DISCLOSURE = "This call is with an AI assistant and may be recorded to help us serve you."


class PrivacyProfile(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """What a business tells people about their data: who to contact, why it is
    collected, how long it is kept. One row per business; the notice shown in the
    chat and on the public privacy page is built from it."""

    __tablename__ = "privacy_profiles"

    legal_name: Mapped[str | None] = mapped_column(String(255))
    officer_name: Mapped[str | None] = mapped_column(String(255))
    officer_title: Mapped[str] = mapped_column(String(80), default="Grievance Officer", nullable=False)
    officer_email: Mapped[str | None] = mapped_column(String(255))
    officer_phone: Mapped[str | None] = mapped_column(String(32))
    address: Mapped[str | None] = mapped_column(Text)
    purposes: Mapped[list] = mapped_column(JSONB, default=lambda: list(DEFAULT_PURPOSES), nullable=False)
    retention_summary: Mapped[str] = mapped_column(Text, default=DEFAULT_RETENTION, nullable=False)
    extra_notice: Mapped[str | None] = mapped_column(Text)
    # Bumped whenever what people are told changes, so each consent records which wording it was given under.
    notice_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    call_disclosure_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=expression.true(), nullable=False
    )
    call_disclosure_text: Mapped[str] = mapped_column(String(300), default=DEFAULT_CALL_DISCLOSURE, nullable=False)
    # The business's own target for answering a request, in days (the law sets the outer limit).
    request_sla_days: Mapped[int] = mapped_column(Integer, default=30, server_default="30", nullable=False)
    # When on, marketing email goes only to people with a granted "marketing" consent on record.
    email_marketing_requires_consent: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=expression.false(), nullable=False
    )

    __table_args__ = (UniqueConstraint("tenant_id", name="uq_privacy_profile_tenant"),)


class ConsentRecord(Base, UUIDPrimaryKeyMixin, TenantScopedMixin):
    """Proof of what someone agreed to, to which wording, when and from where.
    Withdrawal updates the same row, so the history of one agreement stays in one place."""

    __tablename__ = "consent_records"

    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id", ondelete="SET NULL"), index=True
    )
    subject_name: Mapped[str | None] = mapped_column(String(255))
    subject_phone: Mapped[str | None] = mapped_column(String(32))
    subject_email: Mapped[str | None] = mapped_column(String(255))
    purpose: Mapped[str] = mapped_column(String(24), nullable=False)  # enquiry | call_back | marketing | recording
    status: Mapped[str] = mapped_column(String(10), default="granted", nullable=False)  # granted | withdrawn
    source: Mapped[str] = mapped_column(String(24), nullable=False)
    notice_version: Mapped[int | None] = mapped_column(Integer)
    notice_hash: Mapped[str | None] = mapped_column(String(64))
    ip_address: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(String(255))
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    withdrawn_via: Mapped[str | None] = mapped_column(String(24))

    __table_args__ = (
        Index("ix_consent_tenant_phone", "tenant_id", "subject_phone"),
        Index("ix_consent_tenant_email", "tenant_id", "subject_email"),
        Index("ix_consent_tenant_status", "tenant_id", "status"),
    )


class PrivacyRequest(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """A person asking the business to act on their data: see it, correct it,
    erase it, stop using it, or complain. Tracked to a due date."""

    __tablename__ = "privacy_requests"

    reference: Mapped[str] = mapped_column(String(16), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # access | correction | erasure | withdraw_consent | grievance | nominee
    status: Mapped[str] = mapped_column(String(12), default="received", nullable=False)  # received | verifying | in_progress | completed | rejected
    subject_name: Mapped[str] = mapped_column(String(255), nullable=False)
    subject_email: Mapped[str | None] = mapped_column(String(255))
    subject_phone: Mapped[str | None] = mapped_column(String(32))
    details: Mapped[str | None] = mapped_column(Text)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id", ondelete="SET NULL")
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verified_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution: Mapped[str | None] = mapped_column(Text)
    handled_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    ip_address: Mapped[str | None] = mapped_column(String(45))

    __table_args__ = (Index("ix_privacy_requests_tenant_status_due", "tenant_id", "status", "due_at"),)


class DataBreach(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """A personal data breach and the clock that runs from the moment it was found."""

    __tablename__ = "data_breaches"

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    data_categories: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    approx_people: Mapped[int | None] = mapped_column(Integer)
    likely_impact: Mapped[str | None] = mapped_column(Text)
    mitigation: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(10), default="open", nullable=False)  # open | reported | closed
    # Detailed report to the Data Protection Board is due this long after discovery.
    report_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    board_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    board_reference: Mapped[str | None] = mapped_column(String(120))
    principals_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    principals_notified_how: Mapped[str | None] = mapped_column(Text)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    __table_args__ = (Index("ix_data_breaches_tenant_status", "tenant_id", "status"),)

