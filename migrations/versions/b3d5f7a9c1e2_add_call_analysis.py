"""add call analysis fields: summary, sentiment, intent, outcome, action items

Revision ID: b3d5f7a9c1e2
Revises: a9c4e2f7b1d3
Create Date: 2026-10-02 22:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "b3d5f7a9c1e2"
down_revision: Union[str, None] = "a9c4e2f7b1d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("calls", sa.Column("summary", sa.Text()))
    op.add_column("calls", sa.Column("sentiment", sa.String(16)))
    op.add_column("calls", sa.Column("intent", sa.String(160)))
    op.add_column("calls", sa.Column("outcome", sa.String(40)))
    op.add_column("calls", sa.Column("action_items", JSONB()))
    op.add_column("calls", sa.Column("analyzed_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    for col in ("analyzed_at", "action_items", "outcome", "intent", "sentiment", "summary"):
        op.drop_column("calls", col)
