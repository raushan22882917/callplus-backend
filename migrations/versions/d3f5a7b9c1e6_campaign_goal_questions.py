"""Campaigns carry a goal and questions the AI asks; calls keep the answers

Revision ID: d3f5a7b9c1e6
Revises: c2e4a6b8d0f5
Create Date: 2026-11-04 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d3f5a7b9c1e6"
down_revision: Union[str, None] = "c2e4a6b8d0f5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("campaigns", sa.Column("goal", sa.Text(), nullable=True))
    op.add_column(
        "campaigns",
        sa.Column("questions", postgresql.JSONB(), server_default="[]", nullable=False),
    )
    op.add_column("calls", sa.Column("mission", postgresql.JSONB(), nullable=True))
    op.add_column("calls", sa.Column("answers", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("calls", "answers")
    op.drop_column("calls", "mission")
    op.drop_column("campaigns", "questions")
    op.drop_column("campaigns", "goal")
