"""Email suppression list and the marketing-consent switch.

Revision ID: s9k1m3o5j7l2
Revises: r8j0l2n4i6k1
Create Date: 2026-11-12 10:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from packages.database.rls import _enable

revision: str = "s9k1m3o5j7l2"
down_revision: Union[str, None] = "r8j0l2n4i6k1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "email_suppressions",
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("reason", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=24), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "email", name="uq_email_suppression_tenant_email"),
    )
    op.create_index(op.f("ix_email_suppressions_tenant_id"), "email_suppressions", ["tenant_id"], unique=False)
    # Tables made after the first row-level-security migration are not covered by it, so protect this one here.
    _enable(op.get_bind(), "email_suppressions", "tenant_id", nullable_rows_readable=True)
    op.add_column(
        "privacy_profiles",
        sa.Column("email_marketing_requires_consent", sa.Boolean(), server_default=sa.false(), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("privacy_profiles", "email_marketing_requires_consent")
    op.drop_index(op.f("ix_email_suppressions_tenant_id"), table_name="email_suppressions")
    op.drop_table("email_suppressions")
