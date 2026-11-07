import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base
from packages.database.mixins import TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin


class PhoneNumber(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """A phone number the platform's Twilio account owns, assigned to a tenant
    and (optionally) routed to one of that tenant's AI agents. Inbound-call
    routing looks a number up here to find tenant_id — this is the only place
    tenant_id is derived from an inbound webhook rather than a JWT."""

    __tablename__ = "phone_numbers"

    e164_number: Mapped[str] = mapped_column(String(20), unique=True, nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(20), default="twilio", nullable=False)
    provider_sid: Mapped[str | None] = mapped_column(String(64))
    # True when this platform bought the number on its own Twilio account.
    # Deleting the row then releases it at Twilio. A number the tenant brought
    # themselves stays False, so unregistering it does not delete their number.
    platform_owned: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    ai_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ai_agents.id", ondelete="SET NULL")
    )

    __table_args__ = (UniqueConstraint("tenant_id", "e164_number", name="uq_phone_tenant_number"),)


class CallEvent(Base, UUIDPrimaryKeyMixin, TenantScopedMixin):
    """Append-only event stream for a call (CALL_CREATED, TRANSCRIPT_FINAL,
    TOOL_STARTED, ...). High-volume/ephemeral events (audio-level VAD, partial
    transcripts) belong in Redis during the call, not here — this table is for
    events worth keeping after the call ends, for debugging/observability."""

    __tablename__ = "call_events"

    call_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calls.id", ondelete="CASCADE"), nullable=False, index=True
    )
    type: Mapped[str] = mapped_column(String(50), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
