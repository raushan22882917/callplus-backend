from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base
from packages.database.mixins import TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin


class TransferTarget(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """A human a live call can be handed off to (a support rep's phone, a
    team line). When an AI agent calls transfer_call, every active target of
    the tenant is rung at once and the first to answer gets the call."""

    __tablename__ = "transfer_targets"

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    phone_number: Mapped[str] = mapped_column(String(32), nullable=False)  # E.164
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Lower rings first when a tenant has more targets than MAX_RING_TARGETS.
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
