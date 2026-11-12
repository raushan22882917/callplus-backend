import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from packages.database.base import Base
from packages.database.mixins import TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin


class Wallet(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """Prepaid balance for a pay-as-you-go workspace. All amounts are paise.

    The per-minute rates are fixed by the size of the last top-up (bigger top-ups buy a
    lower rate) and stay until the next one. Usage is charged against `balance_paise`;
    every change is a row in `wallet_transactions`."""

    __tablename__ = "wallets"

    balance_paise: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    inbound_rate_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    outbound_rate_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    # Usage before this moment is not charged (it belongs to the free plan the tenant was on).
    billing_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    low_balance_paise: Mapped[int] = mapped_column(Integer, default=20000, nullable=False)
    # For the GST invoice.
    billing_name: Mapped[str | None] = mapped_column(String(255))
    gstin: Mapped[str | None] = mapped_column(String(15))
    state_code: Mapped[str | None] = mapped_column(String(2))
    # Which services this workspace wants billed and allowed. Missing keys stay on.
    services: Mapped[dict] = mapped_column(
        JSONB,
        default=dict,
        server_default='{"calls": true, "whatsapp": true, "sms": true, "email": true}',
        nullable=False,
    )

    __table_args__ = (UniqueConstraint("tenant_id", name="uq_wallet_tenant"),)


class WalletTransaction(Base, UUIDPrimaryKeyMixin, TenantScopedMixin):
    """Append-only ledger. `reference_id` is the call, message, usage event or top-up the
    row is for; the unique constraint is what stops a retry from charging twice."""

    __tablename__ = "wallet_transactions"

    kind: Mapped[str] = mapped_column(String(12), nullable=False)  # topup|call|message|adjustment|refund
    amount_paise: Mapped[int] = mapped_column(Integer, nullable=False)  # + credit, - charge
    balance_after_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    reference_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    detail: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "kind", "reference_id", name="uq_wallet_tx_reference"),
        Index("ix_wallet_tx_tenant_created", "tenant_id", "created_at"),
    )


class WalletTopup(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """One recharge: the Razorpay or PayPal order, what was paid, the GST split and the invoice."""

    __tablename__ = "wallet_topups"

    provider: Mapped[str] = mapped_column(
        String(16), default="razorpay", server_default="razorpay", nullable=False
    )
    provider_order_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    provider_payment_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(10), default="created", nullable=False)  # created|paid|failed
    amount_paise: Mapped[int] = mapped_column(Integer, nullable=False)  # credited to the wallet
    cgst_paise: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sgst_paise: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    igst_paise: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_paise: Mapped[int] = mapped_column(Integer, nullable=False)  # charged: amount + GST
    # What PayPal collected, in US cents, and the rupees-per-dollar rate used. Empty for Razorpay.
    charged_cents: Mapped[int | None] = mapped_column(Integer)
    fx_inr_per_usd: Mapped[int | None] = mapped_column(Integer)
    inbound_rate_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    outbound_rate_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    invoice_number: Mapped[str | None] = mapped_column(String(32), unique=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    billing_name: Mapped[str | None] = mapped_column(String(255))
    gstin: Mapped[str | None] = mapped_column(String(15))
    state_code: Mapped[str | None] = mapped_column(String(2))
    seller_state_code: Mapped[str | None] = mapped_column(String(2))
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

