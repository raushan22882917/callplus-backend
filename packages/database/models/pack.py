import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base
from packages.database.mixins import TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin


class PackPurchase(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """One payment for one or more channel packs (Call, WhatsApp, Email + SMS).

    Prices, allowances and tax are fixed here when the order is made and never read from the
    request. `items` is what was bought, `allowances` is what each pack grants for its period.
    The workspace's live entitlements are `tenants.pack_state`, written only by
    app/services/packs.py when a paid purchase is credited."""

    __tablename__ = "pack_purchases"

    provider: Mapped[str] = mapped_column(String(16), default="razorpay", server_default="razorpay", nullable=False)
    provider_order_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    provider_payment_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(10), default="created", nullable=False)  # created|paid|failed
    items: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)  # ["call", "whatsapp"]
    bundle: Mapped[bool] = mapped_column(default=False, server_default="false", nullable=False)
    allowances: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    period_days: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    amount_paise: Mapped[int] = mapped_column(Integer, nullable=False)  # price before GST
    discount_paise: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # already taken off amount
    cgst_paise: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sgst_paise: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    igst_paise: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_paise: Mapped[int] = mapped_column(Integer, nullable=False)  # charged: amount + GST
    charged_cents: Mapped[int | None] = mapped_column(Integer)  # PayPal: US cents collected
    fx_inr_per_usd: Mapped[int | None] = mapped_column(Integer)
    invoice_number: Mapped[str | None] = mapped_column(String(32), unique=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    billing_name: Mapped[str] = mapped_column(String(255), nullable=False)
    gstin: Mapped[str | None] = mapped_column(String(15))
    state_code: Mapped[str | None] = mapped_column(String(2))
    seller_state_code: Mapped[str | None] = mapped_column(String(2))
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    __table_args__ = (Index("ix_pack_purchases_tenant_created", "tenant_id", "created_at"),)
