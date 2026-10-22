"""Row-level security on user_sessions

The sessions table was added after row-level security was rolled out and was never
covered, although it carries tenant_id: a tenant-scoped query that forgot its filter could
have read another tenant's sessions. Found by tests/test_schema_integrity.py.

Revision ID: b1d3f5a7c9e4
Revises: a0c2e4f6b8d3
Create Date: 2026-10-22 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

from packages.database.rls import _disable, _enable

revision: str = "b1d3f5a7c9e4"
down_revision: Union[str, None] = "a0c2e4f6b8d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    _enable(op.get_bind(), "user_sessions", "tenant_id", nullable_rows_readable=True)


def downgrade() -> None:
    _disable(op.get_bind(), "user_sessions")
