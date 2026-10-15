"""OAuth 2.1 for the MCP server: dynamic clients, auth codes, tokens

Revision ID: b2d4f6a8c0e1
Revises: a1c3e5b7d9f2
Create Date: 2026-10-15 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from packages.database.rls import _disable, _enable

revision: str = "b2d4f6a8c0e1"
down_revision: Union[str, None] = "a1c3e5b7d9f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TENANT_TABLES = ["mcp_oauth_codes", "mcp_oauth_tokens"]


def _ts() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "mcp_oauth_clients",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("client_name", sa.String(200), nullable=False),
        sa.Column("redirect_uris", postgresql.ARRAY(sa.String(2048)), nullable=False),
        *_ts(),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "mcp_oauth_codes",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("hashed_code", sa.String(64), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("scopes", postgresql.ARRAY(sa.String(32)), nullable=False),
        sa.Column("redirect_uri", sa.String(2048), nullable=False),
        sa.Column("code_challenge", sa.String(128), nullable=False),
        sa.Column("resource", sa.String(2048)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        *_ts(),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["client_id"], ["mcp_oauth_clients.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_mcp_oauth_codes_tenant_id", "mcp_oauth_codes", ["tenant_id"])
    op.create_index("ix_mcp_oauth_codes_hashed_code", "mcp_oauth_codes", ["hashed_code"], unique=True)
    op.create_table(
        "mcp_oauth_tokens",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("hashed_access_token", sa.String(64), nullable=False),
        sa.Column("hashed_refresh_token", sa.String(64), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("scopes", postgresql.ARRAY(sa.String(32)), nullable=False),
        sa.Column("resource", sa.String(2048)),
        sa.Column("access_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("refresh_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        *_ts(),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["client_id"], ["mcp_oauth_clients.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_mcp_oauth_tokens_tenant_id", "mcp_oauth_tokens", ["tenant_id"])
    op.create_index(
        "ix_mcp_oauth_tokens_hashed_access_token", "mcp_oauth_tokens", ["hashed_access_token"], unique=True
    )
    op.create_index(
        "ix_mcp_oauth_tokens_hashed_refresh_token", "mcp_oauth_tokens", ["hashed_refresh_token"], unique=True
    )
    conn = op.get_bind()
    for table in TENANT_TABLES:
        _enable(conn, table, "tenant_id", nullable_rows_readable=False)


def downgrade() -> None:
    conn = op.get_bind()
    for table in TENANT_TABLES:
        _disable(conn, table)
    op.drop_table("mcp_oauth_tokens")
    op.drop_table("mcp_oauth_codes")
    op.drop_table("mcp_oauth_clients")
