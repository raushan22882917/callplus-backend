"""add tenants.call_recording_enabled (opt-in call recording)

Revision ID: d5e9a2c7b1f4
Revises: c4d8f1b2a6e3
Create Date: 2026-10-02 00:10:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d5e9a2c7b1f4"
down_revision: Union[str, None] = "c4d8f1b2a6e3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column("call_recording_enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("tenants", "call_recording_enabled")
