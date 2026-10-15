"""seed permissions and system roles

Revision ID: 9141a5e1b2b1
Revises: a9760ba4b7e4
Create Date: 2026-09-18 04:31:00.367353

"""
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

# app.core.permissions is the single source of truth for what each system
# role grants; importing the plain string constants here (not ORM models)
# keeps this migration in sync with it without duplicating the mapping.
REPO_ROOT = Path(__file__).resolve().parents[2]
API_ROOT = str(REPO_ROOT / "apps" / "api")
if API_ROOT not in sys.path:
    sys.path.insert(0, API_ROOT)

from app.core.permissions import ALL_PERMISSIONS_DESCRIPTIONS, DEFAULT_SYSTEM_ROLES  # noqa: E402

# revision identifiers, used by Alembic.
revision: str = "9141a5e1b2b1"
down_revision: str | None = "a9760ba4b7e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

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
    sa.column("is_system", sa.Boolean),
)
role_permissions_table = sa.table(
    "role_permissions",
    sa.column("role_id", UUID(as_uuid=True)),
    sa.column("permission_id", UUID(as_uuid=True)),
)


def upgrade() -> None:
    conn = op.get_bind()

    permission_ids: dict[str, uuid.UUID] = {
        code: uuid.uuid4() for code in ALL_PERMISSIONS_DESCRIPTIONS
    }
    conn.execute(
        permissions_table.insert(),
        [
            {"id": pid, "code": code, "description": ALL_PERMISSIONS_DESCRIPTIONS[code]}
            for code, pid in permission_ids.items()
        ],
    )

    role_ids: dict[str, uuid.UUID] = {name: uuid.uuid4() for name in DEFAULT_SYSTEM_ROLES}
    conn.execute(
        roles_table.insert(),
        [
            {"id": rid, "tenant_id": None, "name": name, "is_system": True}
            for name, rid in role_ids.items()
        ],
    )

    conn.execute(
        role_permissions_table.insert(),
        [
            {"role_id": role_ids[role_name], "permission_id": permission_ids[code]}
            for role_name, codes in DEFAULT_SYSTEM_ROLES.items()
            for code in codes
        ],
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(role_permissions_table.delete())
    conn.execute(roles_table.delete().where(roles_table.c.is_system.is_(True)))
    conn.execute(permissions_table.delete())
