"""Cost controls: per-tenant budget and safety caps, plus alert history

Revision ID: e1a3c5d7f9b2
Revises: d4f6b8c0e2a4
Create Date: 2026-10-20 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from packages.database.rls import _disable, _enable

revision: str = "e1a3c5d7f9b2"
down_revision: Union[str, None] = "d4f6b8c0e2a4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLES = ["cost_controls", "cost_alerts"]


def upgrade() -> None:
    op.create_table(
        "cost_controls",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("monthly_budget", sa.Numeric(14, 2)),
        sa.Column("alert_thresholds", postgresql.JSONB(), nullable=False),
        sa.Column("alert_emails", postgresql.JSONB(), nullable=False),
        sa.Column("pause_outbound_at_budget", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("daily_outbound_call_cap", sa.Integer()),
        sa.Column("max_concurrent_outbound", sa.Integer()),
        sa.Column("alerted_period", sa.String(7)),
        sa.Column("alerted_level", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", name="uq_cost_controls_tenant"),
    )
    op.create_index("ix_cost_controls_tenant_id", "cost_controls", ["tenant_id"])
    op.create_table(
        "cost_alerts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("period", sa.String(7), nullable=False),
        sa.Column("level", sa.SmallInteger(), nullable=False),
        sa.Column("spend", sa.Numeric(14, 2), nullable=False),
        sa.Column("budget", sa.Numeric(14, 2)),
        sa.Column("message", sa.String(300), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_cost_alerts_tenant_id", "cost_alerts", ["tenant_id"])
    op.create_index("ix_cost_alerts_tenant_created", "cost_alerts", ["tenant_id", "created_at"])
    conn = op.get_bind()
    for table in TABLES:
        _enable(conn, table, "tenant_id", nullable_rows_readable=False)


def downgrade() -> None:
    conn = op.get_bind()
    for table in reversed(TABLES):
        _disable(conn, table)
        op.drop_table(table)
