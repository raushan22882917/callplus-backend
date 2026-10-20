from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import expression

from packages.database.base import Base
from packages.database.mixins import TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin


class CostControls(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """Per-business spend budget and safety caps (one row per tenant).

    Amounts are in the tenant's pricing-plan currency. A NULL limit means "no
    limit", so an untouched tenant behaves exactly as before.
    """

    __tablename__ = "cost_controls"

    monthly_budget: Mapped[float | None] = mapped_column(Numeric(14, 2))
    # Percent-of-budget levels that raise an alert, e.g. [50, 80, 100].
    alert_thresholds: Mapped[list] = mapped_column(
        JSONB, default=lambda: [50, 80, 100], nullable=False
    )
    alert_emails: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    # Stop placing outbound calls once the budget is used up (inbound keeps working).
    pause_outbound_at_budget: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=expression.false(), nullable=False
    )
    daily_outbound_call_cap: Mapped[int | None] = mapped_column(Integer)
    max_concurrent_outbound: Mapped[int | None] = mapped_column(Integer)
    # The month and highest threshold already alerted, so each fires once a month.
    alerted_period: Mapped[str | None] = mapped_column(String(7))
    alerted_level: Mapped[int] = mapped_column(
        SmallInteger, default=0, server_default="0", nullable=False
    )

    __table_args__ = (UniqueConstraint("tenant_id", name="uq_cost_controls_tenant"),)


class CostAlert(Base, UUIDPrimaryKeyMixin, TenantScopedMixin):
    """History of budget alerts and spend-guard blocks, newest shown first."""

    __tablename__ = "cost_alerts"

    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # budget | blocked
    period: Mapped[str] = mapped_column(String(7), nullable=False)
    level: Mapped[int] = mapped_column(SmallInteger, default=0, nullable=False)
    spend: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    budget: Mapped[float | None] = mapped_column(Numeric(14, 2))
    message: Mapped[str] = mapped_column(String(300), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=expression.text("now()"), nullable=False
    )

    __table_args__ = (Index("ix_cost_alerts_tenant_created", "tenant_id", "created_at"),)
