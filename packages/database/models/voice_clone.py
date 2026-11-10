import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base
from packages.database.mixins import TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin


class VoiceClone(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """A synthetic copy of a person's voice that this workspace's AI agents may speak with.

    Created from a short recording the speaker made after reading a consent statement (see
    app/services/voice_clones.py). The audio itself is sent to the provider and not kept here; this row keeps the
    provider's voice id, who it belongs to, and the consent that was given, so it can be shown, used and deleted.
    """

    __tablename__ = "voice_clones"

    provider: Mapped[str] = mapped_column(String(20), default="elevenlabs", nullable=False)
    provider_voice_id: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    # Whose voice it is, as the person who recorded it stated it.
    speaker_name: Mapped[str] = mapped_column(String(120), nullable=False)
    language: Mapped[str | None] = mapped_column(String(10))
    # "own": made on the workspace's own provider account. "managed": on CallPlus's account.
    key_source: Mapped[str] = mapped_column(String(10), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="ready", nullable=False)  # ready | needs_verification
    requires_verification: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sample_seconds: Mapped[int | None] = mapped_column()
    consent_version: Mapped[str] = mapped_column(String(40), nullable=False)
    consent_text: Mapped[str] = mapped_column(Text, nullable=False)
    consent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    __table_args__ = (Index("ix_voice_clones_tenant_voice", "tenant_id", "provider_voice_id", unique=True),)
