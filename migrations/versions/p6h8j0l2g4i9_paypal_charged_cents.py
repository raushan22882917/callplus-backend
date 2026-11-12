"""Remember the dollars PayPal collected for a wallet top-up.

Revision ID: p6h8j0l2g4i9
Revises: o5g7i9k1f3h8
Create Date: 2026-11-11 12:30:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "p6h8j0l2g4i9"
down_revision: Union[str, None] = "o5g7i9k1f3h8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("wallet_topups", sa.Column("charged_cents", sa.Integer(), nullable=True))
    op.add_column("wallet_topups", sa.Column("fx_inr_per_usd", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("wallet_topups", "fx_inr_per_usd")
    op.drop_column("wallet_topups", "charged_cents")
