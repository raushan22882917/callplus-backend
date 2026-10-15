"""add billing tables (plans, subscriptions, invoices)

Revision ID: e236fd5c2532
Revises: 15eac5e2529d
Create Date: 2026-09-19 00:05:00.000000

"""
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ENUM, UUID


# revision identifiers, used by Alembic.
revision: str = 'e236fd5c2532'
down_revision: Union[str, None] = '15eac5e2529d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


plans_table = sa.table(
    "plans",
    sa.column("id", UUID(as_uuid=True)),
    sa.column("code", sa.String),
    sa.column("name", sa.String),
    sa.column("price_paise", sa.Integer),
    sa.column("currency", sa.String),
    sa.column("billing_interval", ENUM("MONTH", "YEAR", name="billing_interval", create_type=False)),
    sa.column("is_active", sa.Boolean),
)

DEFAULT_PLANS = [
    {"code": "starter", "name": "Starter", "price_paise": 4900 * 100},
    {"code": "growth", "name": "Growth", "price_paise": 19900 * 100},
    {"code": "scale", "name": "Scale", "price_paise": 49900 * 100},
]


def upgrade() -> None:
    op.create_table(
        'plans',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('code', sa.String(length=50), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('price_paise', sa.Integer(), nullable=False),
        sa.Column('currency', sa.String(length=3), nullable=False),
        sa.Column(
            'billing_interval', sa.Enum('MONTH', 'YEAR', name='billing_interval'), nullable=False
        ),
        sa.Column('provider_plan_id', sa.String(length=64), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code'),
    )
    op.create_index(op.f('ix_plans_code'), 'plans', ['code'], unique=True)

    op.create_table(
        'subscriptions',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('tenant_id', sa.UUID(), nullable=False),
        sa.Column('plan_id', sa.UUID(), nullable=False),
        sa.Column('provider', sa.String(length=20), nullable=False),
        sa.Column('provider_subscription_id', sa.String(length=64), nullable=False),
        sa.Column(
            'status',
            sa.Enum('CREATED', 'ACTIVE', 'PAST_DUE', 'CANCELLED', 'COMPLETED', name='subscription_status'),
            nullable=False,
        ),
        sa.Column('current_period_end', sa.DateTime(timezone=True), nullable=True),
        sa.Column('cancel_at_period_end', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['plan_id'], ['plans.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('provider_subscription_id'),
    )
    op.create_index(op.f('ix_subscriptions_tenant_id'), 'subscriptions', ['tenant_id'], unique=False)
    op.create_index(op.f('ix_subscriptions_plan_id'), 'subscriptions', ['plan_id'], unique=False)
    op.create_index(
        op.f('ix_subscriptions_provider_subscription_id'),
        'subscriptions',
        ['provider_subscription_id'],
        unique=True,
    )

    op.create_table(
        'invoices',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('tenant_id', sa.UUID(), nullable=False),
        sa.Column('subscription_id', sa.UUID(), nullable=True),
        sa.Column('provider_payment_id', sa.String(length=64), nullable=True),
        sa.Column('amount_paise', sa.Integer(), nullable=False),
        sa.Column('currency', sa.String(length=3), nullable=False),
        sa.Column(
            'status', sa.Enum('PENDING', 'PAID', 'FAILED', name='invoice_status'), nullable=False
        ),
        sa.Column('issued_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('paid_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['subscription_id'], ['subscriptions.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('provider_payment_id'),
    )
    op.create_index(op.f('ix_invoices_tenant_id'), 'invoices', ['tenant_id'], unique=False)
    op.create_index(
        op.f('ix_invoices_provider_payment_id'), 'invoices', ['provider_payment_id'], unique=True
    )

    conn = op.get_bind()
    conn.execute(
        plans_table.insert(),
        [
            {
                "id": uuid.uuid4(),
                "code": plan["code"],
                "name": plan["name"],
                "price_paise": plan["price_paise"],
                "currency": "INR",
                "billing_interval": "MONTH",
                "is_active": True,
            }
            for plan in DEFAULT_PLANS
        ],
    )


def downgrade() -> None:
    op.drop_index(op.f('ix_invoices_provider_payment_id'), table_name='invoices')
    op.drop_index(op.f('ix_invoices_tenant_id'), table_name='invoices')
    op.drop_table('invoices')
    op.drop_index(op.f('ix_subscriptions_provider_subscription_id'), table_name='subscriptions')
    op.drop_index(op.f('ix_subscriptions_plan_id'), table_name='subscriptions')
    op.drop_index(op.f('ix_subscriptions_tenant_id'), table_name='subscriptions')
    op.drop_table('subscriptions')
    op.drop_index(op.f('ix_plans_code'), table_name='plans')
    op.drop_table('plans')
    sa.Enum(name='invoice_status').drop(op.get_bind(), checkfirst=True)
    sa.Enum(name='subscription_status').drop(op.get_bind(), checkfirst=True)
    sa.Enum(name='billing_interval').drop(op.get_bind(), checkfirst=True)
