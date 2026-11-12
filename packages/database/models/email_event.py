import uuid

from sqlalchemy import ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base
from packages.database.mixins import TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin


class EmailEvent(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """One open or click on a campaign email.

    Opens come from a one-pixel image and clicks from a signed redirect link, so both are estimates: some mail
    apps load every image in advance (opens run high) and some block images (opens run low). Clicks are
    reliable. ``created_at`` is when it happened.
    """

    __tablename__ = "email_events"

    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False
    )
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id", ondelete="SET NULL")
    )
    kind: Mapped[str] = mapped_column(String(8), nullable=False)  # open | click
    url: Mapped[str | None] = mapped_column(Text)
    user_agent: Mapped[str | None] = mapped_column(String(255))

    __table_args__ = (
        Index("ix_email_events_tenant_campaign_kind", "tenant_id", "campaign_id", "kind"),
        Index("ix_email_events_tenant_created", "tenant_id", "created_at"),
    )
