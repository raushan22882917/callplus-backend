import uuid

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base
from packages.database.mixins import TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin


class WhatsAppAccount(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """A tenant's connected WhatsApp Business number (Meta Cloud API, Tech
    Provider model — one Meta App/webhook shared across tenants, each tenant
    has its own phone_number_id + access token). The token is encrypted at
    rest; see app/core/crypto.py."""

    __tablename__ = "whatsapp_accounts"

    phone_number_id: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )
    waba_id: Mapped[str] = mapped_column(String(64), nullable=False)
    display_phone_number: Mapped[str] = mapped_column(String(20), nullable=False)
    encrypted_access_token: Mapped[str] = mapped_column(String(2000), nullable=False)
    ai_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ai_agents.id", ondelete="SET NULL")
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "phone_number_id", name="uq_whatsapp_tenant_number"),
    )


class WhatsAppTemplate(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """A business's own WhatsApp message template. WhatsApp only lets a business
    start a conversation (or write after 24h of customer silence) with a template
    Meta has approved. The business writes it with named blanks ({{name}},
    {{date}}); we send Meta numbered ones ({{1}}, {{2}}) and keep the order in
    `variables` so sends can fill them from a booking or customer record."""

    __tablename__ = "whatsapp_templates"

    whatsapp_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("whatsapp_accounts.id", ondelete="SET NULL"), index=True
    )
    name: Mapped[str] = mapped_column(String(512), nullable=False)  # Meta's id: lowercase_with_underscores
    language: Mapped[str] = mapped_column(String(16), nullable=False, default="en")
    category: Mapped[str] = mapped_column(String(16), nullable=False, default="UTILITY")
    # What it's for, so automatic sends find it: booking_reminder, payment_link, ... or custom.
    purpose: Mapped[str] = mapped_column(String(32), nullable=False, default="custom")
    header_text: Mapped[str | None] = mapped_column(String(60))
    body: Mapped[str] = mapped_column(Text, nullable=False)  # with named blanks: "Hi {{name}}"
    footer_text: Mapped[str | None] = mapped_column(String(60))
    buttons: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    variables: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)  # ["name", "date"]
    examples: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)  # {"name": "Priya"}
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="DRAFT")
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    meta_template_id: Mapped[str | None] = mapped_column(String(64), index=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "name", "language", name="uq_whatsapp_template_name_language"),
    )
