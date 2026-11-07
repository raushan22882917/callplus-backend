"""Record an A2P declaration and one missed-call recovery attempt

Revision ID: h7e9a1b3c5f0
Revises: g6d8f0a2c4e9
Create Date: 2026-11-07 13:10:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "h7e9a1b3c5f0"
down_revision: Union[str, None] = "g6d8f0a2c4e9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("tenants", sa.Column("a2p_declared_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "missed_calls",
        sa.Column("recovery_attempted_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("missed_calls", "recovery_attempted_at")
    op.drop_column("tenants", "a2p_declared_at")
