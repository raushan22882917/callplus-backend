"""enable Postgres row-level security on tenant tables (defense in depth)

See packages/database/rls.py for the model. The app role must not be a
superuser/BYPASSRLS role for these policies to have any effect.

Revision ID: e6f0b3d8c2a5
Revises: d5e9a2c7b1f4
Create Date: 2026-10-02 00:20:00.000000

"""
from typing import Sequence, Union

from alembic import op

from packages.database.rls import apply_rls, remove_rls

revision: str = "e6f0b3d8c2a5"
down_revision: Union[str, None] = "d5e9a2c7b1f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    apply_rls(op.get_bind())


def downgrade() -> None:
    remove_rls(op.get_bind())
