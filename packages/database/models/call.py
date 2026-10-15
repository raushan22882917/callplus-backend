import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base
from packages.database.mixins import TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin


class CallDirection(str, enum.Enum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class CallStatus(str, enum.Enum):
    QUEUED = "queued"
    RINGING = "ringing"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    NO_ANSWER = "no_answer"


class Call(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """Call summary row owned by the business plane (FastAPI/Postgres).

    The realtime plane (Go gateway) owns the live audio/event stream during
    the call and writes back here — direction, status, timestamps, duration,
    recording — once the call ends. Per-turn transcripts/events live
    elsewhere (Redis during the call, then an events store) and are out of
    scope for this table.
    """

    __tablename__ = "calls"

    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id", ondelete="SET NULL"), index=True
    )
    ai_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ai_agents.id", ondelete="SET NULL")
    )
    # Twilio CallSid — how inbound status-callback webhooks and the realtime
    # gateway's end-of-call notification correlate back to this row.
    provider_call_sid: Mapped[str | None] = mapped_column(
        String(64), unique=True, index=True
    )
    direction: Mapped[CallDirection] = mapped_column(
        Enum(CallDirection, name="call_direction"), nullable=False
    )
    status: Mapped[CallStatus] = mapped_column(
        Enum(CallStatus, name="call_status"), default=CallStatus.QUEUED, nullable=False
    )
    from_number: Mapped[str | None] = mapped_column(String(32))
    to_number: Mapped[str | None] = mapped_column(String(32))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    recording_url: Mapped[str | None] = mapped_column(String(512))
    # Filled by app/services/call_analysis.py once the call ends.
    summary: Mapped[str | None] = mapped_column(Text)
    sentiment: Mapped[str | None] = mapped_column(String(16))  # positive | neutral | negative
    intent: Mapped[str | None] = mapped_column(String(160))
    outcome: Mapped[str | None] = mapped_column(String(40))
    action_items: Mapped[list | None] = mapped_column(JSONB)
    analyzed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_calls_tenant_status", "tenant_id", "status"),
        Index("ix_calls_tenant_customer", "tenant_id", "customer_id"),
    )
