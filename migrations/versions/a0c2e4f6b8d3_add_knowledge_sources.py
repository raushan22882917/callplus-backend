"""Knowledge base: websites as sources (crawl progress, weekly re-sync)

Revision ID: a0c2e4f6b8d3
Revises: f8b0d2e4a6c1
Create Date: 2026-10-21 14:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from packages.database.rls import _disable, _enable

revision: str = "a0c2e4f6b8d3"
down_revision: Union[str, None] = "f8b0d2e4a6c1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "knowledge_sources",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("url", sa.String(2048), nullable=False),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("page_limit", sa.Integer(), nullable=False),
        sa.Column("pages_found", sa.Integer(), nullable=False),
        sa.Column("pages_done", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("resync_weekly", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_knowledge_sources_tenant_id", "knowledge_sources", ["tenant_id"])
    op.add_column("knowledge_documents", sa.Column("source_id", sa.UUID()))
    op.add_column("knowledge_documents", sa.Column("url", sa.String(2048)))
    op.create_foreign_key(
        "fk_knowledge_documents_source",
        "knowledge_documents",
        "knowledge_sources",
        ["source_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_knowledge_documents_source_id", "knowledge_documents", ["source_id"])
    _enable(op.get_bind(), "knowledge_sources", "tenant_id", nullable_rows_readable=False)


def downgrade() -> None:
    _disable(op.get_bind(), "knowledge_sources")
    op.drop_index("ix_knowledge_documents_source_id", table_name="knowledge_documents")
    op.drop_constraint("fk_knowledge_documents_source", "knowledge_documents", type_="foreignkey")
    op.drop_column("knowledge_documents", "url")
    op.drop_column("knowledge_documents", "source_id")
    op.drop_table("knowledge_sources")
