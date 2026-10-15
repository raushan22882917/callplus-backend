import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base
from packages.database.mixins import TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin


class BillingInterval(str, enum.Enum):
    MONTH = "month"
    YEAR = "year"


class SubscriptionStatus(str, enum.Enum):
    CREATED = "created"
    ACTIVE = "active"
    PAST_DUE = "past_due"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


class InvoiceStatus(str, enum.Enum):
    PENDING = "pending"
    PAID = "paid"
    FAILED = "failed"


class Plan(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Platform billing catalog — NOT tenant-scoped, shared across every
    tenant. See app/integrations/razorpay_client.py for how a Plan's
    provider_plan_id is used to create a Razorpay Subscription; it must be
    populated with a real Razorpay Plan ID (created in the dashboard or via
    the Plans API) before checkout can work against a live account."""

    __tablename__ = "plans"

    code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    price_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="INR", nullable=False)
    billing_interval: Mapped[BillingInterval] = mapped_column(
        Enum(BillingInterval, name="billing_interval"),
        default=BillingInterval.MONTH,
        nullable=False,
    )
    provider_plan_id: Mapped[str | None] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Subscription(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """A tenant's subscription to a Plan — platform billing, distinct from
    PaymentLink/Payment in app/models/payment.py, which are a business tool
    tenants use to collect money from *their own* customers."""

    __tablename__ = "subscriptions"

    plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("plans.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(20), default="razorpay", nullable=False)
    provider_subscription_id: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )
    status: Mapped[SubscriptionStatus] = mapped_column(
        Enum(SubscriptionStatus, name="subscription_status"),
        default=SubscriptionStatus.CREATED,
        nullable=False,
    )
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class Invoice(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """A billing charge event for a tenant's Subscription, recorded from a
    Razorpay subscription webhook (see app/api/v1/payments.py)."""

    __tablename__ = "invoices"

    subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscriptions.id", ondelete="SET NULL")
    )
    provider_payment_id: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    amount_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="INR", nullable=False)
    status: Mapped[InvoiceStatus] = mapped_column(
        Enum(InvoiceStatus, name="invoice_status"), default=InvoiceStatus.PENDING, nullable=False
    )
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
