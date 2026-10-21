"""Knowledge base: embeddings (when pgvector exists), source status, answer citations

Revision ID: f8b0d2e4a6c1
Revises: e7a9c1d3f5b8
Create Date: 2026-10-21 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from packages.database.vector_support import ensure_embedding_column

revision: str = "f8b0d2e4a6c1"
down_revision: Union[str, None] = "e7a9c1d3f5b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "knowledge_documents",
        sa.Column("status", sa.String(10), server_default="ready", nullable=False),
    )
    op.add_column("knowledge_documents", sa.Column("error", sa.Text()))
    op.add_column("messages", sa.Column("sources", postgresql.JSONB()))
    # Without the extension the knowledge base keeps working on full-text search alone.
    ensure_embedding_column(op.get_bind())


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_knowledge_chunks_embedding")
    op.execute("ALTER TABLE knowledge_chunks DROP COLUMN IF EXISTS embedding")
    op.drop_column("messages", "sources")
    op.drop_column("knowledge_documents", "error")
    op.drop_column("knowledge_documents", "status")
