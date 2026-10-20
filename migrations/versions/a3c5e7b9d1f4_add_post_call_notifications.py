"""Post-call email and SMS: per-tenant settings and a send log

Revision ID: a3c5e7b9d1f4
Revises: f2b4d6a8c0e1
Create Date: 2026-10-20 18:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from packages.database.rls import _disable, _enable

revision: str = "a3c5e7b9d1f4"
down_revision: Union[str, None] = "f2b4d6a8c0e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLES = ["post_call_settings", "call_notifications"]


def upgrade() -> None:
    op.create_table(
        "post_call_settings",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("customer_sms", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("customer_email", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("staff_email", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("staff_emails", postgresql.JSONB(), nullable=False),
        sa.Column("include_summary", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("min_call_seconds", sa.Integer(), nullable=False),
        sa.Column("sms_message", sa.String(320), nullable=False),
        sa.Column("email_subject", sa.String(150), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", name="uq_post_call_settings_tenant"),
    )
    op.create_index("ix_post_call_settings_tenant_id", "post_call_settings", ["tenant_id"])
    op.create_table(
        "call_notifications",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("call_id", sa.UUID(), nullable=False),
        sa.Column("channel", sa.String(8), nullable=False),
        sa.Column("audience", sa.String(10), nullable=False),
        sa.Column("recipient", sa.String(255), nullable=False),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["call_id"], ["calls.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("call_id", "channel", "audience", "recipient", name="uq_call_notification"),
    )
    op.create_index("ix_call_notifications_tenant_id", "call_notifications", ["tenant_id"])
    op.create_index("ix_call_notifications_tenant_created", "call_notifications", ["tenant_id", "created_at"])
    conn = op.get_bind()
    for table in TABLES:
        _enable(conn, table, "tenant_id", nullable_rows_readable=False)


def downgrade() -> None:
    conn = op.get_bind()
    for table in reversed(TABLES):
        _disable(conn, table)
        op.drop_table(table)
