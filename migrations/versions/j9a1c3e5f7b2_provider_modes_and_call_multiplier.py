"""Workspaces choose CallPlus or their own account per provider; calls remember what they cost in minutes

Revision ID: j9a1c3e5f7b2
Revises: i8f0b2c4d6e1
Create Date: 2026-11-08 20:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "j9a1c3e5f7b2"
down_revision: Union[str, None] = "i8f0b2c4d6e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column("provider_modes", postgresql.JSONB(), server_default="{}", nullable=False),
    )
    op.add_column(
        "calls",
        sa.Column("cost_multiplier", sa.Numeric(6, 2), server_default="1", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("calls", "cost_multiplier")
    op.drop_column("tenants", "provider_modes")
