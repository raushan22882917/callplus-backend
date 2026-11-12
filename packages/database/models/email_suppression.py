from sqlalchemy import String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base
from packages.database.mixins import TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin

REASONS = ("unsubscribed", "bounced", "complaint", "manual")


class EmailSuppression(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """An address a workspace must not send marketing email to.

    One row per workspace and lower-cased address. Added when the person
    unsubscribes, when the provider reports a hard bounce or a spam complaint,
    or by staff. Transactional mail (password reset, invites) ignores it.
    """

    __tablename__ = "email_suppressions"

    email: Mapped[str] = mapped_column(String(255), nullable=False)
    # unsubscribed | bounced | complaint | manual
    reason: Mapped[str] = mapped_column(String(16), nullable=False)
    # unsubscribe_link | one_click | provider_resend | provider_brevo | staff
    source: Mapped[str] = mapped_column(String(24), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (UniqueConstraint("tenant_id", "email", name="uq_email_suppression_tenant_email"),)
