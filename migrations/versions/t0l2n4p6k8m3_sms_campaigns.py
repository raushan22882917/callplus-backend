"""SMS campaigns: the channel value and the message.

Revision ID: t0l2n4p6k8m3
Revises: s9k1m3o5j7l2
Create Date: 2026-11-12 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "t0l2n4p6k8m3"
down_revision: Union[str, None] = "s9k1m3o5j7l2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # New enum labels cannot be used in the same transaction that adds them.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE campaign_channel ADD VALUE IF NOT EXISTS 'SMS'")
    op.add_column("campaigns", sa.Column("sms_body", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("campaigns", "sms_body")
    # PostgreSQL cannot drop a single enum label while rows may still use it.
