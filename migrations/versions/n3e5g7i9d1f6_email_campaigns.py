"""Email campaigns: channel value plus subject, body and reply-to.

Revision ID: n3e5g7i9d1f6
Revises: m2d4f6h8c0e5
Create Date: 2026-11-11 07:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "n3e5g7i9d1f6"
down_revision: Union[str, None] = "m2d4f6h8c0e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # New enum labels cannot be used in the same transaction that adds them.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE campaign_channel ADD VALUE IF NOT EXISTS 'EMAIL'")
    op.add_column("campaigns", sa.Column("email_subject", sa.String(length=200), nullable=True))
    op.add_column("campaigns", sa.Column("email_body", sa.Text(), nullable=True))
    op.add_column("campaigns", sa.Column("email_reply_to", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("campaigns", "email_reply_to")
    op.drop_column("campaigns", "email_body")
    op.drop_column("campaigns", "email_subject")
    # PostgreSQL cannot drop a single enum label while rows may still use it.
