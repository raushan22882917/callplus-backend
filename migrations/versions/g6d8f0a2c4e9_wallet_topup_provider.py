"""Remember whether a wallet top-up was paid with Razorpay or PayPal

Revision ID: g6d8f0a2c4e9
Revises: f5c7e9a1b3d8
Create Date: 2026-11-07 12:45:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "g6d8f0a2c4e9"
down_revision: Union[str, None] = "f5c7e9a1b3d8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "wallet_topups",
        sa.Column("provider", sa.String(length=16), nullable=False, server_default="razorpay"),
    )


def downgrade() -> None:
    op.drop_column("wallet_topups", "provider")
