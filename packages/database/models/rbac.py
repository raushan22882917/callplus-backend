import enum
import uuid

from sqlalchemy import Column, Enum, ForeignKey, String, Table, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from packages.database.base import Base
from packages.database.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class MembershipStatus(str, enum.Enum):
    INVITED = "invited"
    ACTIVE = "active"
    DISABLED = "disabled"


role_permissions = Table(
    "role_permissions",
    Base.metadata,
    Column(
        "role_id", UUID(as_uuid=True), ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
    ),
    Column(
        "permission_id",
        UUID(as_uuid=True),
        ForeignKey("permissions.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


class Role(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "roles"

    # NULL tenant_id = a system role available to every tenant (OWNER, ADMIN, ...).
    # Non-null = a tenant's own custom role.
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    is_system: Mapped[bool] = mapped_column(default=False, nullable=False)

    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_role_tenant_name"),)

    permissions: Mapped[list["Permission"]] = relationship(
        secondary=role_permissions, back_populates="roles"
    )


class Permission(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "permissions"

    code: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(String(255))

    roles: Mapped[list["Role"]] = relationship(
        secondary=role_permissions, back_populates="permissions"
    )


class TenantUser(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A user's membership in a tenant — the sole source of truth for tenant_id
    once a request is authenticated."""

    __tablename__ = "tenant_users"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("roles.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[MembershipStatus] = mapped_column(
        Enum(MembershipStatus, name="membership_status"),
        default=MembershipStatus.ACTIVE,
        nullable=False,
    )

    __table_args__ = (UniqueConstraint("tenant_id", "user_id", name="uq_tenant_user"),)

    tenant: Mapped["Tenant"] = relationship(back_populates="memberships")  # noqa: F821
    user: Mapped["User"] = relationship(back_populates="memberships")  # noqa: F821
    role: Mapped["Role"] = relationship()
