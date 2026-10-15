"""answering-machine detection: agent settings and call verdict

Revision ID: c3e5a7b9d1f3
Revises: b2d4f6a8c0e1
Create Date: 2026-10-15 15:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c3e5a7b9d1f3"
down_revision: Union[str, None] = "b2d4f6a8c0e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ai_agents",
        sa.Column("detect_voicemail", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column("ai_agents", sa.Column("voicemail_message", sa.String(500), nullable=True))
    op.add_column("calls", sa.Column("answered_by", sa.String(32), nullable=True))


def downgrade() -> None:
    op.drop_column("calls", "answered_by")
    op.drop_column("ai_agents", "voicemail_message")
    op.drop_column("ai_agents", "detect_voicemail")
