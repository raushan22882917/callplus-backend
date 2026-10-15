from datetime import datetime

from sqlalchemy import DateTime, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base
from packages.database.mixins import TenantScopedMixin, UUIDPrimaryKeyMixin


class BlockedCaller(Base, UUIDPrimaryKeyMixin, TenantScopedMixin):
    """A phone number this tenant's AI agents refuse to answer. Populated
    either manually (staff) or automatically by the inbound voice webhook
    when a caller crosses the tenant's spam_call_threshold within
    spam_call_window_minutes (see Tenant model and app/api/v1/telephony.py).
    """

    __tablename__ = "blocked_callers"

    e164_number: Mapped[str] = mapped_column(String(20), nullable=False)
    reason: Mapped[str] = mapped_column(String(255), default="manual", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "e164_number", name="uq_blocked_caller_tenant_number"),
    )
