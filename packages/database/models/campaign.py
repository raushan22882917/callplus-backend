import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base
from packages.database.mixins import TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin


class CampaignChannel(str, enum.Enum):
    VOICE = "voice"
    WHATSAPP = "whatsapp"


class CampaignStatus(str, enum.Enum):
    DRAFT = "draft"
    SCHEDULED = "scheduled"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"


class CampaignContactStatus(str, enum.Enum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    COMPLETED = "completed"


class Campaign(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    __tablename__ = "campaigns"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    channel: Mapped[CampaignChannel] = mapped_column(
        Enum(CampaignChannel, name="campaign_channel"), nullable=False
    )
    status: Mapped[CampaignStatus] = mapped_column(
        Enum(CampaignStatus, name="campaign_status"), default=CampaignStatus.DRAFT, nullable=False
    )
    ai_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ai_agents.id", ondelete="SET NULL")
    )
    # For a WhatsApp campaign, an approved template name (Meta requires a
    # template to message outside the 24h customer-service window).
    whatsapp_template_name: Mapped[str | None] = mapped_column(String(255))
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_campaigns_tenant_status", "tenant_id", "status"),)


class CampaignContact(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    __tablename__ = "campaign_contacts"

    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("customers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[CampaignContactStatus] = mapped_column(
        Enum(CampaignContactStatus, name="campaign_contact_status"),
        default=CampaignContactStatus.PENDING,
        nullable=False,
    )
    attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    __table_args__ = (
        Index("ix_campaign_contacts_tenant_campaign_status", "tenant_id", "campaign_id", "status"),
    )
