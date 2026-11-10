"""Voice clones: a person's voice, with the consent given, that agents may speak with

Revision ID: k0b2d4f6a8c3
Revises: j9a1c3e5f7b2
Create Date: 2026-11-09 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from packages.database.rls import _enable

revision: str = "k0b2d4f6a8c3"
down_revision: Union[str, None] = "j9a1c3e5f7b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "voice_clones",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(20), nullable=False, server_default="elevenlabs"),
        sa.Column("provider_voice_id", sa.String(100), nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("speaker_name", sa.String(120), nullable=False),
        sa.Column("language", sa.String(10), nullable=True),
        sa.Column("key_source", sa.String(10), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="ready"),
        sa.Column("requires_verification", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("sample_seconds", sa.Integer(), nullable=True),
        sa.Column("consent_version", sa.String(40), nullable=False),
        sa.Column("consent_text", sa.Text(), nullable=False),
        sa.Column("consent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_voice_clones_tenant_id", "voice_clones", ["tenant_id"])
    op.create_index("ix_voice_clones_tenant_voice", "voice_clones", ["tenant_id", "provider_voice_id"], unique=True)
    # Tables made after the first row-level-security migration are not covered by it, so protect this one here.
    _enable(op.get_bind(), "voice_clones", "tenant_id", nullable_rows_readable=True)


def downgrade() -> None:
    op.drop_index("ix_voice_clones_tenant_voice", table_name="voice_clones")
    op.drop_index("ix_voice_clones_tenant_id", table_name="voice_clones")
    op.drop_table("voice_clones")
