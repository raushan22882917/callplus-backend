import enum
import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base
from packages.database.mixins import TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin


class HumanAgentStatus(str, enum.Enum):
    AVAILABLE = "available"
    BUSY = "busy"
    AWAY = "away"
    OFFLINE = "offline"


class AgentProfile(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """A human support agent's routing profile (one per tenant member). Drives
    app/services/routing.py: only AVAILABLE agents with a phone_number and spare
    capacity are rung on a transfer."""

    __tablename__ = "agent_profiles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[HumanAgentStatus] = mapped_column(
        Enum(HumanAgentStatus, name="human_agent_status"), default=HumanAgentStatus.OFFLINE, nullable=False
    )
    phone_number: Mapped[str | None] = mapped_column(String(32))  # E.164, where calls ring
    skills: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    languages: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    departments: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    is_vip_handler: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    max_concurrent: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (UniqueConstraint("tenant_id", "user_id", name="uq_agent_profile_user"),)


class BusinessHours(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """One row per tenant. `windows` is a list of
    {"weekday": 0-6 (Mon=0), "start": "HH:MM", "end": "HH:MM"} in `timezone`."""

    __tablename__ = "business_hours"

    timezone: Mapped[str] = mapped_column(String(50), default="UTC", nullable=False)
    windows: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    # What to do when nobody can take a transfer: "voicemail" | "callback"
    fallback_action: Mapped[str] = mapped_column(String(16), default="voicemail", nullable=False)
    fallback_message: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (UniqueConstraint("tenant_id", name="uq_business_hours_tenant"),)


class Holiday(Base, UUIDPrimaryKeyMixin, TenantScopedMixin):
    __tablename__ = "holidays"

    day: Mapped[date] = mapped_column(Date, nullable=False)
    name: Mapped[str] = mapped_column(String(120), default="", nullable=False)

    __table_args__ = (UniqueConstraint("tenant_id", "day", name="uq_holiday_tenant_day"),)


class HandlerState(str, enum.Enum):
    AI = "ai"
    HUMAN = "human"


class CallHandoff(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """Who is currently handling a call. No row (or state=ai) means the AI
    agent replies; state=human makes /internal/calls/{id}/turn return no reply."""

    __tablename__ = "call_handoffs"

    call_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calls.id", ondelete="CASCADE"), nullable=False
    )
    state: Mapped[HandlerState] = mapped_column(
        Enum(HandlerState, name="handler_state"), default=HandlerState.AI, nullable=False
    )
    handler_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    conference_name: Mapped[str | None] = mapped_column(String(80))
    agent_call_sid: Mapped[str | None] = mapped_column(String(64), index=True)
    on_hold: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    muted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    taken_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    returned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    handoff_reason: Mapped[str | None] = mapped_column(String(255))

    __table_args__ = (UniqueConstraint("call_id", name="uq_call_handoff_call"),)
