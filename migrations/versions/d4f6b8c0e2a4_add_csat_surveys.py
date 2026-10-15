"""CSAT surveys: per-tenant settings and survey rows

Revision ID: d4f6b8c0e2a4
Revises: c3e5a7b9d1f3
Create Date: 2026-10-16 10:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from packages.database.rls import _disable, _enable

revision: str = "d4f6b8c0e2a4"
down_revision: Union[str, None] = "c3e5a7b9d1f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLES = ["csat_settings", "csat_surveys"]


def _ts() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "csat_settings",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("after_calls", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("after_tickets", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("channel", sa.String(12), nullable=False),
        sa.Column("delay_minutes", sa.Integer(), nullable=False),
        sa.Column("cooldown_hours", sa.Integer(), nullable=False),
        sa.Column("expire_hours", sa.Integer(), nullable=False),
        sa.Column("min_call_seconds", sa.Integer(), nullable=False),
        sa.Column("low_score_ticket_threshold", sa.Integer(), nullable=False),
        sa.Column("question", sa.String(600), nullable=False),
        sa.Column("thank_you", sa.String(300), nullable=False),
        *_ts(),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", name="uq_csat_settings_tenant"),
    )
    op.create_index("ix_csat_settings_tenant_id", "csat_settings", ["tenant_id"])
    op.create_table(
        "csat_surveys",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("customer_id", sa.UUID(), nullable=False),
        sa.Column("source_type", sa.String(10), nullable=False),
        sa.Column("source_id", sa.UUID(), nullable=False),
        sa.Column("channel", sa.String(12)),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("send_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("answered_at", sa.DateTime(timezone=True)),
        sa.Column("rating", sa.SmallInteger()),
        sa.Column("comment", sa.Text()),
        sa.Column("error", sa.Text()),
        *_ts(),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "source_type", "source_id", name="uq_csat_source"),
    )
    op.create_index("ix_csat_surveys_tenant_id", "csat_surveys", ["tenant_id"])
    op.create_index("ix_csat_surveys_due", "csat_surveys", ["status", "send_at"])
    conn = op.get_bind()
    for table in TABLES:
        _enable(conn, table, "tenant_id", nullable_rows_readable=False)


def downgrade() -> None:
    conn = op.get_bind()
    for table in reversed(TABLES):
        _disable(conn, table)
        op.drop_table(table)
