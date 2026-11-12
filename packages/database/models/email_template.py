import uuid

from sqlalchemy import String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base
from packages.database.mixins import TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin


class EmailTemplate(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """A saved subject and message a campaign can start from."""

    __tablename__ = "email_templates"

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    subject: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_email_template_tenant_name"),)
