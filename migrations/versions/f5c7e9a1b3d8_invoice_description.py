"""Describe an invoice line, so an overage bill says what it is for

Revision ID: f5c7e9a1b3d8
Revises: e4b6d8f0a2c7
Create Date: 2026-11-07 12:40:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f5c7e9a1b3d8"
down_revision: Union[str, None] = "e4b6d8f0a2c7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("invoices", sa.Column("description", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("invoices", "description")
