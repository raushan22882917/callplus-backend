"""add tenants.plan_tier / plan_overrides and reprice the platform plan catalogue

Existing tenants keep 'legacy' (unlimited) so nothing changes under them; new
sign-ups get the tier named by SIGNUP_PLAN_TIER ('free').

The `plans` rows (Razorpay catalogue) are repriced ONLY where provider_plan_id is
unset: a plan already linked to a Razorpay plan has its price fixed on Razorpay's
side, so changing it here would make the app and the invoice disagree. Re-create
those plans in Razorpay and update provider_plan_id by hand.

Revision ID: c5e7a9b1d3f6
Revises: b4d6f8a0c2e5
Create Date: 2026-10-21 10:00:00.000000

"""
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c5e7a9b1d3f6"
down_revision: Union[str, None] = "b4d6f8a0c2e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# code, name, interval, price in rupees. Annual = ten months' price (two free),
# stored per year. See docs/BILLING.md and scripts/cost_model.py for why.
CATALOGUE = [
    ("starter", "Starter", "MONTH", 2999),
    ("growth", "Growth", "MONTH", 12999),
    ("scale", "Scale", "MONTH", 54999),
    ("starter_annual", "Starter (annual)", "YEAR", 29990),
    ("growth_annual", "Growth (annual)", "YEAR", 129990),
    ("scale_annual", "Scale (annual)", "YEAR", 549990),
]


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column("plan_tier", sa.String(20), server_default="legacy", nullable=False),
    )
    op.add_column(
        "tenants",
        sa.Column(
            "plan_overrides",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
    )

    conn = op.get_bind()
    for code, name, interval, rupees in CATALOGUE:
        exists = conn.execute(sa.text("SELECT provider_plan_id FROM plans WHERE code = :c"), {"c": code}).first()
        if exists is None:
            conn.execute(
                sa.text(
                    "INSERT INTO plans (id, code, name, price_paise, currency, billing_interval, is_active) "
                    "VALUES (:id, :code, :name, :paise, 'INR', CAST(:interval AS billing_interval), true)"
                ),
                {"id": uuid.uuid4(), "code": code, "name": name, "paise": rupees * 100, "interval": interval},
            )
        elif exists[0] is None:
            conn.execute(
                sa.text("UPDATE plans SET name = :name, price_paise = :paise WHERE code = :code"),
                {"code": code, "name": name, "paise": rupees * 100},
            )


def downgrade() -> None:
    op.drop_column("tenants", "plan_overrides")
    op.drop_column("tenants", "plan_tier")
