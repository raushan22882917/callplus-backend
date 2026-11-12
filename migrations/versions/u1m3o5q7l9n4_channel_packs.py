"""Channel packs: the purchases, and the live pack state on the workspace.

Revision ID: u1m3o5q7l9n4
Revises: t0l2n4p6k8m3
Create Date: 2026-11-12 15:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from packages.database.rls import _enable

revision: str = "u1m3o5q7l9n4"
down_revision: Union[str, None] = "t0l2n4p6k8m3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column("pack_state", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
    )
    op.create_table(
        "pack_purchases",
        sa.Column("provider", sa.String(length=16), server_default="razorpay", nullable=False),
        sa.Column("provider_order_id", sa.String(length=64), nullable=False),
        sa.Column("provider_payment_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("items", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("bundle", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("allowances", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("period_days", sa.Integer(), nullable=False),
        sa.Column("amount_paise", sa.Integer(), nullable=False),
        sa.Column("discount_paise", sa.Integer(), nullable=False),
        sa.Column("cgst_paise", sa.Integer(), nullable=False),
        sa.Column("sgst_paise", sa.Integer(), nullable=False),
        sa.Column("igst_paise", sa.Integer(), nullable=False),
        sa.Column("total_paise", sa.Integer(), nullable=False),
        sa.Column("charged_cents", sa.Integer(), nullable=True),
        sa.Column("fx_inr_per_usd", sa.Integer(), nullable=True),
        sa.Column("invoice_number", sa.String(length=32), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("billing_name", sa.String(length=255), nullable=False),
        sa.Column("gstin", sa.String(length=15), nullable=True),
        sa.Column("state_code", sa.String(length=2), nullable=True),
        sa.Column("seller_state_code", sa.String(length=2), nullable=True),
        sa.Column("created_by_user_id", sa.UUID(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider_order_id"),
        sa.UniqueConstraint("provider_payment_id"),
        sa.UniqueConstraint("invoice_number"),
    )
    op.create_index(op.f("ix_pack_purchases_tenant_id"), "pack_purchases", ["tenant_id"], unique=False)
    op.create_index("ix_pack_purchases_tenant_created", "pack_purchases", ["tenant_id", "created_at"], unique=False)
    # Tables made after the first row-level-security migration are not covered by it, so protect this one here.
    _enable(op.get_bind(), "pack_purchases", "tenant_id", nullable_rows_readable=True)


def downgrade() -> None:
    op.drop_index("ix_pack_purchases_tenant_created", table_name="pack_purchases")
    op.drop_index(op.f("ix_pack_purchases_tenant_id"), table_name="pack_purchases")
    op.drop_table("pack_purchases")
    op.drop_column("tenants", "pack_state")
