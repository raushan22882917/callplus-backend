import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import expression

from packages.database.base import Base
from packages.database.mixins import TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin

DEFAULT_QUESTION = (
    "Thanks for contacting us! How satisfied were you? "
    "Reply with a number from 1 (very unhappy) to 5 (very happy). "
    "You can add a comment after the number."
)
DEFAULT_THANK_YOU = "Thank you for your feedback!"


class CsatSettings(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """Per-business CSAT survey configuration (one row per tenant)."""

    __tablename__ = "csat_settings"

    enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=expression.false(), nullable=False
    )
    after_calls: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=expression.true(), nullable=False
    )
    after_tickets: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=expression.true(), nullable=False
    )
    # auto = WhatsApp while its 24h window is open, otherwise SMS.
    channel: Mapped[str] = mapped_column(String(12), default="auto", nullable=False)
    delay_minutes: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    cooldown_hours: Mapped[int] = mapped_column(Integer, default=24, nullable=False)
    expire_hours: Mapped[int] = mapped_column(Integer, default=48, nullable=False)
    min_call_seconds: Mapped[int] = mapped_column(Integer, default=15, nullable=False)
    # Ratings at or below this open a high-priority ticket (0 turns that off).
    low_score_ticket_threshold: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    question: Mapped[str] = mapped_column(String(600), default=DEFAULT_QUESTION, nullable=False)
    thank_you: Mapped[str] = mapped_column(String(300), default=DEFAULT_THANK_YOU, nullable=False)

    __table_args__ = (UniqueConstraint("tenant_id", name="uq_csat_settings_tenant"),)


class CsatSurvey(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """One survey for one finished call or resolved ticket."""

    __tablename__ = "csat_surveys"

    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id", ondelete="CASCADE"), nullable=False
    )
    source_type: Mapped[str] = mapped_column(String(10), nullable=False)  # call | ticket
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    channel: Mapped[str | None] = mapped_column(String(12))  # whatsapp | sms, set when sent
    # pending -> sent -> answered | expired ; pending -> failed
    status: Mapped[str] = mapped_column(String(10), default="pending", nullable=False)
    send_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rating: Mapped[int | None] = mapped_column(SmallInteger)
    comment: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("tenant_id", "source_type", "source_id", name="uq_csat_source"),
        Index("ix_csat_surveys_due", "status", "send_at"),
    )
