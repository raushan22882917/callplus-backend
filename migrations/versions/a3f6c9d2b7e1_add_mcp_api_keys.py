"""add mcp_api_keys table and mcp.manage permission

Revision ID: a3f6c9d2b7e1
Revises: 9ee657b5944b
Create Date: 2026-09-27 00:00:00.000000

"""
import sys
import uuid
from pathlib import Path
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

REPO_ROOT = Path(__file__).resolve().parents[2]
API_ROOT = str(REPO_ROOT / "apps" / "api")
if API_ROOT not in sys.path:
    sys.path.insert(0, API_ROOT)

from app.core.permissions import ALL_PERMISSIONS_DESCRIPTIONS, DEFAULT_SYSTEM_ROLES  # noqa: E402

# revision identifiers, used by Alembic.
revision: str = "a3f6c9d2b7e1"
down_revision: Union[str, None] = "9ee657b5944b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

permissions_table = sa.table(
    "permissions",
    sa.column("id", UUID(as_uuid=True)),
    sa.column("code", sa.String),
    sa.column("description", sa.String),
)
roles_table = sa.table(
    "roles",
    sa.column("id", UUID(as_uuid=True)),
    sa.column("tenant_id", UUID(as_uuid=True)),
    sa.column("name", sa.String),
)
role_permissions_table = sa.table(
    "role_permissions",
    sa.column("role_id", UUID(as_uuid=True)),
    sa.column("permission_id", UUID(as_uuid=True)),
)


def upgrade() -> None:
    op.create_table(
        "mcp_api_keys",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("key_prefix", sa.String(length=20), nullable=False),
        sa.Column("hashed_key", sa.String(length=64), nullable=False),
        sa.Column("created_by_user_id", sa.UUID(), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("hashed_key"),
    )
    op.create_index("ix_mcp_api_keys_tenant_id", "mcp_api_keys", ["tenant_id"])
    op.create_index("ix_mcp_api_keys_hashed_key", "mcp_api_keys", ["hashed_key"], unique=True)

    conn = op.get_bind()

    existing_codes = {row.code for row in conn.execute(sa.select(permissions_table.c.code))}
    new_codes = set(ALL_PERMISSIONS_DESCRIPTIONS) - existing_codes
    if new_codes:
        conn.execute(
            permissions_table.insert(),
            [
                {
                    "id": uuid.uuid4(),
                    "code": code,
                    "description": ALL_PERMISSIONS_DESCRIPTIONS[code],
                }
                for code in new_codes
            ],
        )

    permission_ids = {
        row.code: row.id
        for row in conn.execute(sa.select(permissions_table.c.id, permissions_table.c.code))
    }
    role_ids = {
        row.name: row.id
        for row in conn.execute(
            sa.select(roles_table.c.id, roles_table.c.name).where(roles_table.c.tenant_id.is_(None))
        )
    }
    existing_links = {
        (row.role_id, row.permission_id)
        for row in conn.execute(
            sa.select(role_permissions_table.c.role_id, role_permissions_table.c.permission_id)
        )
    }

    new_links = [
        {"role_id": role_ids[role_name], "permission_id": permission_ids[code]}
        for role_name, codes in DEFAULT_SYSTEM_ROLES.items()
        for code in codes
        if (role_ids[role_name], permission_ids[code]) not in existing_links
    ]
    if new_links:
        conn.execute(role_permissions_table.insert(), new_links)


def downgrade() -> None:
    op.drop_index("ix_mcp_api_keys_hashed_key", table_name="mcp_api_keys")
    op.drop_index("ix_mcp_api_keys_tenant_id", table_name="mcp_api_keys")
    op.drop_table("mcp_api_keys")
    # Permission/role seed rows are additive-only; deliberately a no-op,
    # matching fb765bc75eb8/9ee657b5944b.
