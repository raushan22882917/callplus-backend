import enum
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Enum, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from packages.database.base import Base
from packages.database.mixins import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from packages.database.models.rbac import TenantUser


class TenantStatus(str, enum.Enum):
    TRIAL = "trial"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    CANCELLED = "cancelled"


class Tenant(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "tenants"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    industry: Mapped[str | None] = mapped_column(String(100))
    timezone: Mapped[str] = mapped_column(String(50), default="UTC", nullable=False)
    country: Mapped[str | None] = mapped_column(String(2))
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    status: Mapped[TenantStatus] = mapped_column(
        Enum(TenantStatus, name="tenant_status"),
        default=TenantStatus.TRIAL,
        nullable=False,
    )

    # Spam-call protection — owner-configurable (see app/api/v1/telephony.py's
    # inbound handler, where a caller crossing the threshold within the
    # window gets auto-added to blocked_callers and rejected on every call
    # after). Tenant-wide, not per-agent: abuse of one number is abuse of
    # the business, regardless of which agent answered.
    spam_protection_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    spam_call_window_minutes: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    spam_call_threshold: Mapped[int] = mapped_column(Integer, default=5, nullable=False)

    # Opt-in: recording calls carries consent/legal obligations that vary by
    # jurisdiction, so it is off until the tenant explicitly enables it (and
    # is responsible for any "this call may be recorded" disclosure, e.g. in
    # the agent greeting).
    call_recording_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )

    memberships: Mapped[list["TenantUser"]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan"
    )
