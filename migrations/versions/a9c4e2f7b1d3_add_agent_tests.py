"""add Flow Studio test cases and test runs

Revision ID: a9c4e2f7b1d3
Revises: f1a7c3e9d2b4
Create Date: 2026-10-02 16:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from packages.database.rls import _disable, _enable

revision: str = "a9c4e2f7b1d3"
down_revision: Union[str, None] = "f1a7c3e9d2b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ts = dict(server_default=sa.text("now()"), nullable=False)


def upgrade() -> None:
    op.create_table(
        "agent_test_cases",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("ai_agents.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("persona", sa.String(4000), nullable=False),
        sa.Column("opening", sa.String(1000), nullable=False, server_default=""),
        sa.Column("variables", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("checks", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("max_turns", sa.Integer(), nullable=False, server_default="8"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), **_ts),
        sa.Column("updated_at", sa.DateTime(timezone=True), **_ts),
    )
    op.create_table(
        "agent_test_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("ai_agents.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("case_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agent_test_cases.id", ondelete="SET NULL"), index=True),
        sa.Column("case_name", sa.String(200), nullable=False),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), index=True),
        sa.Column("flow_version", sa.Integer()),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("passed", sa.Boolean()),
        sa.Column("results", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("transcript", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("total_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.String(2000)),
        sa.Column("created_at", sa.DateTime(timezone=True), **_ts),
        sa.Column("updated_at", sa.DateTime(timezone=True), **_ts),
    )
    conn = op.get_bind()
    for table in ("agent_test_cases", "agent_test_runs"):
        _enable(conn, table, "tenant_id", nullable_rows_readable=False)


def downgrade() -> None:
    conn = op.get_bind()
    for table in ("agent_test_runs", "agent_test_cases"):
        _disable(conn, table)
        op.drop_table(table)
