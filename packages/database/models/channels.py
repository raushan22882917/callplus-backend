import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base
from packages.database.mixins import TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin

# Values of ChannelSettings.channel. A plain string (not a Postgres enum) so a
# new channel never needs a type migration.
SETTINGS_CHANNELS = ("sms", "email", "web_chat", "web_form", "voicemail")


class ChannelSettings(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """One row per (tenant, channel): on/off, which AI agent answers, public
    config (widget colours, allowed origins...) and encrypted credentials.

    `public_key` is the unguessable id embedded in public URLs (web chat
    widget, web forms, email webhook) — it is how an unauthenticated request
    is mapped to a tenant. `external_id` is the provider-side identifier an
    inbound webhook carries."""

    __tablename__ = "channel_settings"

    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    public_key: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    external_id: Mapped[str | None] = mapped_column(String(128), index=True)
    ai_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ai_agents.id", ondelete="SET NULL")
    )
    config: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    # Fernet-encrypted JSON object (app/core/crypto.py); never returned by the API.
    encrypted_credentials: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (UniqueConstraint("tenant_id", "channel", name="uq_channel_settings"),)


class ChannelThread(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """Channel-specific metadata for a Conversation: who we are talking to on
    the other end and how to reach them again (phone, email, Meta PSID, web
    chat session), plus the staff read marker the unified inbox uses."""

    __tablename__ = "channel_threads"

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    # Stable key of the remote party/thread: phone number, PSID, sha256 of the
    # web chat session token, or the root Message-ID of an email thread.
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    # Where replies go: phone number / email address / PSID.
    contact_address: Mapped[str | None] = mapped_column(String(255))
    # Our side: the Twilio number / Facebook page id the thread arrived on.
    via_address: Mapped[str | None] = mapped_column(String(255))
    subject: Mapped[str | None] = mapped_column(String(255))
    last_read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("tenant_id", "channel", "external_id", name="uq_channel_thread_key"),
        Index("ix_channel_threads_tenant_channel", "tenant_id", "channel"),
    )


class MissedCall(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """An inbound call nobody answered (no-answer/busy/failed, or routed to
    voicemail). `handled` is the staff follow-up marker."""

    __tablename__ = "missed_calls"

    call_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calls.id", ondelete="SET NULL"), index=True
    )
    provider_call_sid: Mapped[str | None] = mapped_column(String(64), index=True)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id", ondelete="SET NULL"), index=True
    )
    from_number: Mapped[str | None] = mapped_column(String(32))
    to_number: Mapped[str | None] = mapped_column(String(32))
    # no-answer | busy | failed | voicemail
    reason: Mapped[str] = mapped_column(String(20), nullable=False)
    handled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    handled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    handled_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    callback_call_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calls.id", ondelete="SET NULL")
    )
    # Set once we have tried to call them back, so a failure is not retried forever.
    recovery_attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_missed_calls_tenant_handled", "tenant_id", "handled"),)


class Voicemail(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    __tablename__ = "voicemails"

    missed_call_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("missed_calls.id", ondelete="SET NULL"), index=True
    )
    provider_call_sid: Mapped[str | None] = mapped_column(String(64), index=True)
    provider_recording_sid: Mapped[str | None] = mapped_column(String(64), unique=True)
    from_number: Mapped[str | None] = mapped_column(String(32))
    to_number: Mapped[str | None] = mapped_column(String(32))
    recording_url: Mapped[str | None] = mapped_column(String(512))
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    transcription: Mapped[str | None] = mapped_column(Text)
    # pending | completed | failed
    transcription_status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    handled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    handled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    handled_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    __table_args__ = (Index("ix_voicemails_tenant_handled", "tenant_id", "handled"),)
