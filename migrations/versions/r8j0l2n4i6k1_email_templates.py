"""Saved email messages a campaign can start from.

Revision ID: r8j0l2n4i6k1
Revises: q7i9k1m3h5j0
Create Date: 2026-11-11 19:10:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from packages.database.rls import _enable

revision: str = "r8j0l2n4i6k1"
down_revision: Union[str, None] = "q7i9k1m3h5j0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "email_templates",
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("subject", sa.String(length=200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "name", name="uq_email_template_tenant_name"),
    )
    op.create_index(op.f("ix_email_templates_tenant_id"), "email_templates", ["tenant_id"], unique=False)
    # Tables made after the first row-level-security migration are not covered by it, so protect this one here.
    _enable(op.get_bind(), "email_templates", "tenant_id", nullable_rows_readable=True)


def downgrade() -> None:
    op.drop_index(op.f("ix_email_templates_tenant_id"), table_name="email_templates")
    op.drop_table("email_templates")
