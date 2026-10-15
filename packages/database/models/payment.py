import enum
import uuid

from sqlalchemy import Enum, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base
from packages.database.mixins import TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin


class PaymentLinkStatus(str, enum.Enum):
    CREATED = "created"
    PAID = "paid"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class PaymentStatus(str, enum.Enum):
    CAPTURED = "captured"
    FAILED = "failed"
    REFUNDED = "refunded"


class PaymentLink(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """A Razorpay Payment Link a tenant's AI agent (or staff) creates to
    collect payment from a customer during/after a call or WhatsApp chat —
    a business tool, not platform-billing. See app/integrations/razorpay.py."""

    __tablename__ = "payment_links"

    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id", ondelete="SET NULL"), index=True
    )
    provider: Mapped[str] = mapped_column(String(20), default="razorpay", nullable=False)
    provider_payment_link_id: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )
    amount_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="INR", nullable=False)
    description: Mapped[str | None] = mapped_column(String(500))
    short_url: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[PaymentLinkStatus] = mapped_column(
        Enum(PaymentLinkStatus, name="payment_link_status"),
        default=PaymentLinkStatus.CREATED,
        nullable=False,
    )


class Payment(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """A captured/failed payment event, recorded from a Razorpay webhook."""

    __tablename__ = "payments"

    payment_link_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payment_links.id", ondelete="SET NULL")
    )
    provider: Mapped[str] = mapped_column(String(20), default="razorpay", nullable=False)
    provider_payment_id: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )
    amount_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="INR", nullable=False)
    status: Mapped[PaymentStatus] = mapped_column(
        Enum(PaymentStatus, name="payment_status"), nullable=False
    )
    method: Mapped[str | None] = mapped_column(String(30))
