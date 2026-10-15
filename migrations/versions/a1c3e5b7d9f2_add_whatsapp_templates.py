"""WhatsApp message templates owned by each business

Revision ID: a1c3e5b7d9f2
Revises: f7b9d1e3a5c4
Create Date: 2026-10-03 13:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from packages.database.rls import _disable, _enable

revision: str = "a1c3e5b7d9f2"
down_revision: Union[str, None] = "f7b9d1e3a5c4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "whatsapp_templates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "whatsapp_account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("whatsapp_accounts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("name", sa.String(512), nullable=False),
        sa.Column("language", sa.String(16), nullable=False, server_default="en"),
        sa.Column("category", sa.String(16), nullable=False, server_default="UTILITY"),
        sa.Column("purpose", sa.String(32), nullable=False, server_default="custom"),
        sa.Column("header_text", sa.String(60), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("footer_text", sa.String(60), nullable=True),
        sa.Column("buttons", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("variables", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("examples", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(16), nullable=False, server_default="DRAFT"),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("meta_template_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("tenant_id", "name", "language", name="uq_whatsapp_template_name_language"),
    )
    op.create_index("ix_whatsapp_templates_tenant_id", "whatsapp_templates", ["tenant_id"])
    op.create_index("ix_whatsapp_templates_whatsapp_account_id", "whatsapp_templates", ["whatsapp_account_id"])
    op.create_index("ix_whatsapp_templates_meta_template_id", "whatsapp_templates", ["meta_template_id"])
    _enable(op.get_bind(), "whatsapp_templates", "tenant_id", nullable_rows_readable=False)


def downgrade() -> None:
    _disable(op.get_bind(), "whatsapp_templates")
    op.drop_table("whatsapp_templates")
