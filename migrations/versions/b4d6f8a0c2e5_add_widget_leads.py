"""Website chat leads: contact, attribution, call consent and call outcome

Revision ID: b4d6f8a0c2e5
Revises: a3c5e7b9d1f4
Create Date: 2026-10-20 20:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from packages.database.rls import _disable, _enable

revision: str = "b4d6f8a0c2e5"
down_revision: Union[str, None] = "a3c5e7b9d1f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "widget_leads",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("conversation_id", sa.UUID()),
        sa.Column("customer_id", sa.UUID()),
        sa.Column("crm_lead_id", sa.UUID()),
        sa.Column("name", sa.String(255)),
        sa.Column("phone", sa.String(32)),
        sa.Column("email", sa.String(255)),
        sa.Column("interest", sa.Text()),
        sa.Column("page_url", sa.String(2048)),
        sa.Column("referrer", sa.String(2048)),
        sa.Column("utm", postgresql.JSONB(), nullable=False),
        sa.Column("user_agent", sa.String(255)),
        sa.Column("ip_address", sa.String(45)),
        sa.Column("consent_call", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("consent_text", sa.String(600)),
        sa.Column("consent_at", sa.DateTime(timezone=True)),
        sa.Column("consent_ip", sa.String(45)),
        sa.Column("call_status", sa.String(12), server_default="none", nullable=False),
        sa.Column("call_id", sa.UUID()),
        sa.Column("call_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("call_note", sa.String(500)),
        sa.Column("call_requested_at", sa.DateTime(timezone=True)),
        sa.Column("last_call_error", sa.String(300)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["crm_lead_id"], ["crm_leads.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["call_id"], ["calls.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("conversation_id"),
    )
    op.create_index("ix_widget_leads_tenant_id", "widget_leads", ["tenant_id"])
    op.create_index("ix_widget_leads_customer_id", "widget_leads", ["customer_id"])
    op.create_index("ix_widget_leads_crm_lead_id", "widget_leads", ["crm_lead_id"])
    op.create_index("ix_widget_leads_call_id", "widget_leads", ["call_id"])
    op.create_index("ix_widget_leads_tenant_created", "widget_leads", ["tenant_id", "created_at"])
    op.create_index("ix_widget_leads_tenant_call_status", "widget_leads", ["tenant_id", "call_status"])
    op.create_index("ix_widget_leads_tenant_phone", "widget_leads", ["tenant_id", "phone"])
    _enable(op.get_bind(), "widget_leads", "tenant_id", nullable_rows_readable=False)


def downgrade() -> None:
    _disable(op.get_bind(), "widget_leads")
    op.drop_table("widget_leads")
