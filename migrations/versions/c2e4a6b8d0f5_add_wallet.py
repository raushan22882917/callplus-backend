"""Prepaid wallet: balance, ledger and top-ups with GST invoices

Revision ID: c2e4a6b8d0f5
Revises: b1d3f5a7c9e4
Create Date: 2026-10-24 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from packages.database.rls import _disable, _enable

revision: str = "c2e4a6b8d0f5"
down_revision: Union[str, None] = "b1d3f5a7c9e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLES = ["wallets", "wallet_transactions", "wallet_topups"]


def _ts():
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "wallets",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("balance_paise", sa.Integer(), nullable=False),
        sa.Column("inbound_rate_paise", sa.Integer(), nullable=False),
        sa.Column("outbound_rate_paise", sa.Integer(), nullable=False),
        sa.Column("billing_started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("low_balance_paise", sa.Integer(), nullable=False),
        sa.Column("billing_name", sa.String(255)),
        sa.Column("gstin", sa.String(15)),
        sa.Column("state_code", sa.String(2)),
        *_ts(),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", name="uq_wallet_tenant"),
    )
    op.create_index("ix_wallets_tenant_id", "wallets", ["tenant_id"])

    op.create_table(
        "wallet_transactions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(12), nullable=False),
        sa.Column("amount_paise", sa.Integer(), nullable=False),
        sa.Column("balance_after_paise", sa.Integer(), nullable=False),
        sa.Column("reference_id", sa.UUID()),
        sa.Column("description", sa.String(255), nullable=False),
        sa.Column("detail", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "kind", "reference_id", name="uq_wallet_tx_reference"),
    )
    op.create_index("ix_wallet_transactions_tenant_id", "wallet_transactions", ["tenant_id"])
    op.create_index("ix_wallet_tx_tenant_created", "wallet_transactions", ["tenant_id", "created_at"])

    op.create_table(
        "wallet_topups",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("provider_order_id", sa.String(64), nullable=False),
        sa.Column("provider_payment_id", sa.String(64)),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("amount_paise", sa.Integer(), nullable=False),
        sa.Column("cgst_paise", sa.Integer(), nullable=False),
        sa.Column("sgst_paise", sa.Integer(), nullable=False),
        sa.Column("igst_paise", sa.Integer(), nullable=False),
        sa.Column("total_paise", sa.Integer(), nullable=False),
        sa.Column("inbound_rate_paise", sa.Integer(), nullable=False),
        sa.Column("outbound_rate_paise", sa.Integer(), nullable=False),
        sa.Column("invoice_number", sa.String(32)),
        sa.Column("paid_at", sa.DateTime(timezone=True)),
        sa.Column("billing_name", sa.String(255)),
        sa.Column("gstin", sa.String(15)),
        sa.Column("state_code", sa.String(2)),
        sa.Column("seller_state_code", sa.String(2)),
        sa.Column("created_by_user_id", sa.UUID()),
        *_ts(),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider_order_id"),
        sa.UniqueConstraint("provider_payment_id"),
        sa.UniqueConstraint("invoice_number"),
    )
    op.create_index("ix_wallet_topups_tenant_id", "wallet_topups", ["tenant_id"])

    conn = op.get_bind()
    for table in TABLES:
        _enable(conn, table, "tenant_id", nullable_rows_readable=False)


def downgrade() -> None:
    conn = op.get_bind()
    for table in reversed(TABLES):
        _disable(conn, table)
        op.drop_table(table)
