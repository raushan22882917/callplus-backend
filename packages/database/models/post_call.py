import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import expression

from packages.database.base import Base
from packages.database.mixins import TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin

DEFAULT_SMS = "Thanks for calling {business}. {summary}"
DEFAULT_EMAIL_SUBJECT = "Thanks for calling {business}"


class PostCallSettings(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """What gets sent after a call ends (one row per business). Off until enabled."""

    __tablename__ = "post_call_settings"

    enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=expression.false(), nullable=False
    )
    customer_sms: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=expression.true(), nullable=False
    )
    customer_email: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=expression.true(), nullable=False
    )
    staff_email: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=expression.false(), nullable=False
    )
    staff_emails: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    include_summary: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=expression.true(), nullable=False
    )
    # Calls shorter than this (a wrong number, a hang-up) send nothing.
    min_call_seconds: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    sms_message: Mapped[str] = mapped_column(String(320), default=DEFAULT_SMS, nullable=False)
    email_subject: Mapped[str] = mapped_column(
        String(150), default=DEFAULT_EMAIL_SUBJECT, nullable=False
    )

    __table_args__ = (UniqueConstraint("tenant_id", name="uq_post_call_settings_tenant"),)


class CallNotification(Base, UUIDPrimaryKeyMixin, TenantScopedMixin):
    """One message sent (or skipped, or failed) about a call. The unique key is
    also the guard against sending the same message twice."""

    __tablename__ = "call_notifications"

    call_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calls.id", ondelete="CASCADE"), nullable=False
    )
    channel: Mapped[str] = mapped_column(String(8), nullable=False)  # sms | email
    audience: Mapped[str] = mapped_column(String(10), nullable=False)  # customer | staff
    recipient: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False)  # sent | failed | skipped
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=expression.text("now()"), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("call_id", "channel", "audience", "recipient", name="uq_call_notification"),
        Index("ix_call_notifications_tenant_created", "tenant_id", "created_at"),
    )
