"""Per-customer daily and weekly call caps on campaign dialer settings.

Revision ID: m2d4f6h8c0e5
Revises: l1c3e5g7b9d4
Create Date: 2026-11-10 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "m2d4f6h8c0e5"
down_revision: Union[str, None] = "l1c3e5g7b9d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for column in ("per_customer_daily_cap", "per_customer_weekly_cap"):
        op.add_column("campaign_dialer_settings", sa.Column(column, sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("campaign_dialer_settings", "per_customer_weekly_cap")
    op.drop_column("campaign_dialer_settings", "per_customer_daily_cap")
