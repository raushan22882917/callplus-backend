"""add developer platform: api keys, usage, webhooks, idempotency

Revision ID: f1a7c3e9d2b4
Revises: e6f0b3d8c2a5
Create Date: 2026-10-02 13:30:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from packages.database.rls import _enable, _disable

revision: str = "f1a7c3e9d2b4"
down_revision: Union[str, None] = "e6f0b3d8c2a5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NEW_TABLES = [
    "api_keys",
    "api_key_usage_daily",
    "webhook_endpoints",
    "webhook_deliveries",
    "idempotency_records",
]


def upgrade() -> None:
    ts = lambda: sa.Column(  # noqa: E731
        "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )
    ts_updated = lambda: sa.Column(  # noqa: E731
        "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )

    op.create_table(
        "api_keys",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("key_prefix", sa.String(24), nullable=False),
        sa.Column("hashed_key", sa.String(64), nullable=False),
        sa.Column("mode", sa.String(8), nullable=False),
        sa.Column("scopes", sa.ARRAY(sa.String(32)), nullable=False),
        sa.Column("created_by_user_id", sa.UUID(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        ts(),
        ts_updated(),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("hashed_key"),
    )
    op.create_index("ix_api_keys_tenant_id", "api_keys", ["tenant_id"])
    op.create_index("ix_api_keys_hashed_key", "api_keys", ["hashed_key"], unique=True)

    op.create_table(
        "api_key_usage_daily",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("api_key_id", sa.UUID(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("requests", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["api_key_id"], ["api_keys.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("api_key_id", "day", name="uq_api_key_usage_key_day"),
    )
    op.create_index("ix_api_key_usage_daily_tenant_id", "api_key_usage_daily", ["tenant_id"])

    op.create_table(
        "webhook_endpoints",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("url", sa.String(2048), nullable=False),
        sa.Column("events", sa.ARRAY(sa.String(48)), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("signing_secret_encrypted", sa.Text(), nullable=False),
        ts(),
        ts_updated(),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_webhook_endpoints_tenant_id", "webhook_endpoints", ["tenant_id"])

    op.create_table(
        "webhook_deliveries",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("endpoint_id", sa.UUID(), nullable=False),
        sa.Column("event", sa.String(48), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("response_code", sa.Integer(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(500), nullable=True),
        ts(),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["endpoint_id"], ["webhook_endpoints.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_webhook_deliveries_tenant_id", "webhook_deliveries", ["tenant_id"])
    op.create_index(
        "ix_webhook_deliveries_tenant_created", "webhook_deliveries", ["tenant_id", "created_at"]
    )

    op.create_table(
        "idempotency_records",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("key", sa.String(128), nullable=False),
        sa.Column("call_id", sa.UUID(), nullable=False),
        ts(),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["call_id"], ["calls.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "key", name="uq_idempotency_tenant_key"),
    )
    op.create_index("ix_idempotency_records_tenant_id", "idempotency_records", ["tenant_id"])

    # Same tenant-isolation backstop every other tenant table got in e6f0b3d8c2a5.
    conn = op.get_bind()
    for table in NEW_TABLES:
        _enable(conn, table, "tenant_id", nullable_rows_readable=True)


def downgrade() -> None:
    conn = op.get_bind()
    for table in reversed(NEW_TABLES):
        _disable(conn, table)
    for table in reversed(NEW_TABLES):
        op.drop_table(table)
