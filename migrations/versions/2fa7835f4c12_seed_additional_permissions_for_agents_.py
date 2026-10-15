"""seed additional permissions for agents, workflows, campaigns, integrations, payments

Revision ID: 2fa7835f4c12
Revises: 1d3d8f5209bb
Create Date: 2026-09-18 05:10:00.000000

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
revision: str = "2fa7835f4c12"
down_revision: Union[str, None] = "1d3d8f5209bb"
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
    conn = op.get_bind()

    existing_codes = {row.code for row in conn.execute(sa.select(permissions_table.c.code))}
    new_codes = set(ALL_PERMISSIONS_DESCRIPTIONS) - existing_codes
    if new_codes:
        conn.execute(
            permissions_table.insert(),
            [
                {"id": uuid.uuid4(), "code": code, "description": ALL_PERMISSIONS_DESCRIPTIONS[code]}
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
    # Additive-only migration (new permission codes + new role grants); no
    # data existed before it to restore, so downgrade is a deliberate no-op
    # rather than deleting permissions that later migrations may depend on.
    pass
