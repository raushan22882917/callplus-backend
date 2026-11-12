"""Email opens and clicks.

Revision ID: v2n4p6r8m0o5
Revises: u1m3o5q7l9n4
Create Date: 2026-11-12 20:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql  # noqa: F401
from alembic import op

from packages.database.rls import _enable

revision: str = "v2n4p6r8m0o5"
down_revision: Union[str, None] = "u1m3o5q7l9n4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column("email_autopilot", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
    )
    op.create_table(
        "email_events",
        sa.Column("campaign_id", sa.UUID(), nullable=False),
        sa.Column("customer_id", sa.UUID(), nullable=True),
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("user_agent", sa.String(length=255), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_email_events_tenant_id"), "email_events", ["tenant_id"], unique=False)
    op.create_index("ix_email_events_tenant_campaign_kind", "email_events", ["tenant_id", "campaign_id", "kind"], unique=False)
    op.create_index("ix_email_events_tenant_created", "email_events", ["tenant_id", "created_at"], unique=False)
    # Tables made after the first row-level-security migration are not covered by it, so protect this one here.
    _enable(op.get_bind(), "email_events", "tenant_id", nullable_rows_readable=True)


def downgrade() -> None:
    op.drop_column("tenants", "email_autopilot")
    op.drop_index("ix_email_events_tenant_created", table_name="email_events")
    op.drop_index("ix_email_events_tenant_campaign_kind", table_name="email_events")
    op.drop_index(op.f("ix_email_events_tenant_id"), table_name="email_events")
    op.drop_table("email_events")
