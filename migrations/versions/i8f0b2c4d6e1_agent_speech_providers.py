"""Agents choose who hears and who speaks: Sarvam, Deepgram or ElevenLabs

Revision ID: i8f0b2c4d6e1
Revises: h7e9a1b3c5f0
Create Date: 2026-11-08 18:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "i8f0b2c4d6e1"
down_revision: Union[str, None] = "h7e9a1b3c5f0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ai_agents",
        sa.Column("stt_provider", sa.String(length=20), server_default="sarvam", nullable=False),
    )
    op.add_column(
        "ai_agents",
        sa.Column("tts_provider", sa.String(length=20), server_default="sarvam", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("ai_agents", "tts_provider")
    op.drop_column("ai_agents", "stt_provider")
