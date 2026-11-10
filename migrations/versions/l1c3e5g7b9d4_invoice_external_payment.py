"""Remember which payment settled an invoice, without replacing the overage key.

Revision ID: l1c3e5g7b9d4
Revises: k0b2d4f6a8c3
Create Date: 2026-11-10 10:10:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "l1c3e5g7b9d4"
down_revision: Union[str, None] = "k0b2d4f6a8c3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("invoices", sa.Column("external_payment_id", sa.String(length=64), nullable=True))
    op.create_index("ix_invoices_external_payment_id", "invoices", ["external_payment_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_invoices_external_payment_id", table_name="invoices")
    op.drop_column("invoices", "external_payment_id")
