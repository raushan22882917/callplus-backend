import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base
from packages.database.mixins import TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin


class ConversationChannel(str, enum.Enum):
    VOICE = "voice"
    WHATSAPP = "whatsapp"
    INTERNAL = "internal"
    # Omnichannel (app/api/v1/channels.py). Postgres enum `conversation_channel`
    # needs ALTER TYPE ... ADD VALUE for each of these in a migration.
    SMS = "sms"
    EMAIL = "email"
    WEB_CHAT = "web_chat"
    INSTAGRAM = "instagram"
    MESSENGER = "messenger"
    WEB_FORM = "web_form"


class MessageDirection(str, enum.Enum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class MessageSender(str, enum.Enum):
    CUSTOMER = "customer"
    AI_AGENT = "ai_agent"
    HUMAN_AGENT = "human_agent"
    STAFF_USER = "staff_user"


class Conversation(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """One conversation thread per channel interaction — a phone call or a
    WhatsApp thread. `reference_id` points at the channel-specific record
    (calls.id for voice; there's no separate WhatsApp thread table, so for
    WhatsApp it's just this row's own id)."""

    __tablename__ = "conversations"

    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id", ondelete="SET NULL"), index=True
    )
    channel: Mapped[ConversationChannel] = mapped_column(
        Enum(ConversationChannel, name="conversation_channel"), nullable=False
    )
    call_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calls.id", ondelete="SET NULL"), index=True
    )
    # Only set for channel=INTERNAL — scopes an internal-assistant thread to the
    # staff member who started it. Null for voice/WhatsApp conversations.
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    # Human takeover: while this is in the future the AI agent stays quiet and
    # staff answer from the inbox. Set when staff reply; cleared by "hand back to AI".
    ai_paused_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_conversations_tenant_customer", "tenant_id", "customer_id"),)


class Message(Base, UUIDPrimaryKeyMixin, TenantScopedMixin):
    __tablename__ = "messages"

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    direction: Mapped[MessageDirection] = mapped_column(
        Enum(MessageDirection, name="message_direction"), nullable=False
    )
    sender: Mapped[MessageSender] = mapped_column(
        Enum(MessageSender, name="message_sender"), nullable=False
    )
    text: Mapped[str | None] = mapped_column(Text)
    media_url: Mapped[str | None] = mapped_column(String(512))
    provider_message_id: Mapped[str | None] = mapped_column(String(128), index=True)
    # Outbound delivery as the provider reports it (WhatsApp: sent, delivered,
    # read, failed). delivery_error is the provider's reason when it failed.
    delivery_status: Mapped[str | None] = mapped_column(String(16))
    delivery_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index(
            "ix_messages_tenant_conversation_created",
            "tenant_id",
            "conversation_id",
            "created_at",
        ),
    )
