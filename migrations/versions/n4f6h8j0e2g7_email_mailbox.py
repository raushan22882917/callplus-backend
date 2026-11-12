"""A workspace mailbox: Gmail, Microsoft, or manual IMAP/SMTP.

Revision ID: n4f6h8j0e2g7
Revises: n3e5g7i9d1f6
Create Date: 2026-11-11 08:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from packages.database.rls import _enable

revision: str = "n4f6h8j0e2g7"
down_revision: Union[str, None] = "n3e5g7i9d1f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "email_mailboxes",
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("from_email", sa.String(length=255), nullable=False),
        sa.Column("from_name", sa.String(length=120), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("smtp_host", sa.String(length=255), nullable=True),
        sa.Column("smtp_port", sa.Integer(), nullable=True),
        sa.Column("smtp_username", sa.String(length=255), nullable=True),
        sa.Column("encrypted_smtp_password", sa.Text(), nullable=True),
        sa.Column("smtp_use_tls", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("imap_host", sa.String(length=255), nullable=True),
        sa.Column("imap_port", sa.Integer(), nullable=True),
        sa.Column("imap_username", sa.String(length=255), nullable=True),
        sa.Column("encrypted_imap_password", sa.Text(), nullable=True),
        sa.Column("imap_use_ssl", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("imap_uidvalidity", sa.String(length=40), nullable=True),
        sa.Column("imap_last_uid", sa.Integer(), nullable=True),
        sa.Column("last_inbound_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", name="uq_email_mailbox_tenant"),
    )
    op.create_index(op.f("ix_email_mailboxes_tenant_id"), "email_mailboxes", ["tenant_id"], unique=False)
    # Tables made after the first row-level-security migration are not covered by it, so protect this one here.
    _enable(op.get_bind(), "email_mailboxes", "tenant_id", nullable_rows_readable=True)


def downgrade() -> None:
    op.drop_index(op.f("ix_email_mailboxes_tenant_id"), table_name="email_mailboxes")
    op.drop_table("email_mailboxes")
