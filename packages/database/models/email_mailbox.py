"""The mailbox a workspace sends and receives customer email from.

One row per workspace. OAuth tokens stay in oauth_connections; this row only
records which account is active and, for a manual mailbox, the encrypted
IMAP/SMTP password.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base
from packages.database.mixins import TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin


class EmailMailbox(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    __tablename__ = "email_mailboxes"

    # gmail | microsoft | smtp
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    from_email: Mapped[str] = mapped_column(String(255), nullable=False)
    from_name: Mapped[str | None] = mapped_column(String(120))
    # connected | error
    status: Mapped[str] = mapped_column(String(16), default="connected", nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)

    smtp_host: Mapped[str | None] = mapped_column(String(255))
    smtp_port: Mapped[int | None] = mapped_column(Integer)
    smtp_username: Mapped[str | None] = mapped_column(String(255))
    encrypted_smtp_password: Mapped[str | None] = mapped_column(Text)
    smtp_use_tls: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    imap_host: Mapped[str | None] = mapped_column(String(255))
    imap_port: Mapped[int | None] = mapped_column(Integer)
    imap_username: Mapped[str | None] = mapped_column(String(255))
    encrypted_imap_password: Mapped[str | None] = mapped_column(Text)
    imap_use_ssl: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    imap_uidvalidity: Mapped[str | None] = mapped_column(String(40))
    imap_last_uid: Mapped[int | None] = mapped_column(Integer)

    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (UniqueConstraint("tenant_id", name="uq_email_mailbox_tenant"),)
