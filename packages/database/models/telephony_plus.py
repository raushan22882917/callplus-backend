"""Contact-center telephony upgrades: IVR/auto-attendant, queues, ring groups,
forwarding, DND registry, per-campaign dialer settings + outcomes, SIP trunks
and number routing. Every table is tenant-scoped."""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base
from packages.database.mixins import TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin


class IvrMenu(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """Auto-attendant for a number. `options` maps a dialed digit to an action:
    {"1": {"action": "queue", "target_id": "<queue uuid>", "label": "Sales"}}.
    Actions: ai_agent (target_id), queue (target_id), ring_group (target_id),
    forward (number), voicemail (message), submenu (target_id), hangup."""

    __tablename__ = "ivr_menus"

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    number: Mapped[str | None] = mapped_column(String(20), index=True)  # E.164 it answers
    greeting_text: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str] = mapped_column(String(16), default="en-IN", nullable=False)
    options: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=6, nullable=False)
    max_retries: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    # Action dict (same shape as an option) used outside business hours.
    after_hours_action: Mapped[dict | None] = mapped_column(JSONB)
    after_hours_message: Mapped[str | None] = mapped_column(Text)


class CallQueue(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    __tablename__ = "queues"

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    strategy: Mapped[str] = mapped_column(String(24), default="round_robin", nullable=False)
    max_wait_seconds: Mapped[int] = mapped_column(Integer, default=300, nullable=False)
    # {"action": "voicemail|forward|ring_group|ai_agent|hangup", "number"/"target_id"/"message"}
    overflow_action: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    hold_message: Mapped[str] = mapped_column(
        Text, default="Please hold, your call is important to us.", nullable=False
    )
    hold_music_url: Mapped[str | None] = mapped_column(String(512))
    # E.164 numbers rung (via Twilio REST) to dequeue callers.
    members: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)

    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_queue_tenant_name"),)


class RingGroup(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    __tablename__ = "ring_groups"

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    members: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)  # E.164 numbers
    strategy: Mapped[str] = mapped_column(String(16), default="simultaneous", nullable=False)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=20, nullable=False)

    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_ring_group_tenant_name"),)


class CallForwardingRule(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """Applied to inbound calls to `number` before IVR/AI routing. `condition`:
    always | after_hours. Lowest `priority` wins."""

    __tablename__ = "call_forwarding_rules"

    number: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    condition: Mapped[str] = mapped_column(String(16), default="always", nullable=False)
    destination_type: Mapped[str] = mapped_column(String(16), default="number", nullable=False)
    destination: Mapped[str] = mapped_column(String(64), nullable=False)  # E.164 or uuid
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class DndEntry(Base, UUIDPrimaryKeyMixin, TenantScopedMixin):
    """The tenant's do-not-disturb registry; checked before every outbound dial."""

    __tablename__ = "dnd_registry"

    e164_number: Mapped[str] = mapped_column(String(20), nullable=False)
    source: Mapped[str] = mapped_column(String(32), default="manual", nullable=False)
    note: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (UniqueConstraint("tenant_id", "e164_number", name="uq_dnd_tenant_number"),)


class CampaignDialerSettings(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    __tablename__ = "campaign_dialer_settings"

    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    mode: Mapped[str] = mapped_column(String(16), default="progressive", nullable=False)
    pacing_ratio: Mapped[float] = mapped_column(Float, default=1.5, nullable=False)
    max_concurrent: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    abandon_rate_cap: Mapped[float] = mapped_column(Float, default=0.03, nullable=False)
    default_timezone: Mapped[str] = mapped_column(String(50), default="Asia/Kolkata", nullable=False)
    call_window_start: Mapped[str] = mapped_column(String(5), default="09:00", nullable=False)
    call_window_end: Mapped[str] = mapped_column(String(5), default="21:00", nullable=False)
    quiet_hours_start: Mapped[str] = mapped_column(String(5), default="21:00", nullable=False)
    quiet_hours_end: Mapped[str] = mapped_column(String(5), default="09:00", nullable=False)
    daily_call_limit: Mapped[int | None] = mapped_column(Integer)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    # Minutes to wait before attempt 2, 3, ...; the last value repeats.
    retry_schedule_minutes: Mapped[list] = mapped_column(
        JSONB, default=lambda: [60, 240, 1440], nullable=False
    )
    route_qualified_to_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )


class CampaignContactState(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """Dialer bookkeeping per campaign contact (attempts, next retry/callback)."""

    __tablename__ = "campaign_contact_states"

    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("campaign_contacts.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_call_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    last_processed_call_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    last_disposition: Mapped[str | None] = mapped_column(String(32))

    __table_args__ = (Index("ix_contact_state_campaign", "tenant_id", "campaign_id"),)


class CampaignOutcome(Base, UUIDPrimaryKeyMixin, TenantScopedMixin):
    """One disposition per dial attempt (auto-derived from the call, or set by
    an agent). Source of truth for stats, the CSV export and abandon rate."""

    __tablename__ = "campaign_outcomes"

    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaign_contacts.id", ondelete="CASCADE"), nullable=False
    )
    call_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calls.id", ondelete="SET NULL")
    )
    disposition: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[str] = mapped_column(String(16), default="auto", nullable=False)
    qualified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    callback_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_campaign_outcomes_campaign", "tenant_id", "campaign_id"),)


class SipTrunk(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    __tablename__ = "sip_trunks"

    label: Mapped[str] = mapped_column(String(120), nullable=False)
    provider: Mapped[str] = mapped_column(String(60), default="twilio", nullable=False)
    termination_uri: Mapped[str] = mapped_column(String(255), nullable=False)
    # Name of the secret/integration the credentials live in; never the secret.
    credentials_ref: Mapped[str | None] = mapped_column(String(120))
    caller_id: Mapped[str | None] = mapped_column(String(20))
    origination_uri: Mapped[str | None] = mapped_column(String(255))
    twilio_trunk_sid: Mapped[str | None] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sync_status: Mapped[str] = mapped_column(String(16), default="unsynced", nullable=False)
    sync_error: Mapped[str | None] = mapped_column(Text)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NumberRouting(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """Maps one of the tenant's numbers to a department and a target:
    ivr | ai_agent | queue | ring_group | user (target_id)."""

    __tablename__ = "number_routing"

    e164_number: Mapped[str] = mapped_column(String(20), nullable=False)
    department: Mapped[str | None] = mapped_column(String(120))
    target_type: Mapped[str] = mapped_column(String(16), nullable=False)
    target_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    __table_args__ = (UniqueConstraint("tenant_id", "e164_number", name="uq_number_routing"),)


class ParkedCall(Base, UUIDPrimaryKeyMixin, TenantScopedMixin):
    __tablename__ = "parked_calls"

    call_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calls.id", ondelete="CASCADE"), nullable=False
    )
    slot: Mapped[str] = mapped_column(String(64), nullable=False)
    parked_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    parked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    retrieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_parked_calls_tenant_open", "tenant_id", "retrieved_at"),)
