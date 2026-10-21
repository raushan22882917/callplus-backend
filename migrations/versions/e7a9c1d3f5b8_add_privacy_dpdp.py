"""DPDP: privacy profile, consent ledger, data-principal requests, breach register

Revision ID: e7a9c1d3f5b8
Revises: d6f8b0c2e4a7
Create Date: 2026-10-20 20:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from packages.database.rls import _disable, _enable

revision: str = "e7a9c1d3f5b8"
down_revision: Union[str, None] = "d6f8b0c2e4a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLES = ["privacy_profiles", "consent_records", "privacy_requests", "data_breaches"]


def _ts():
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "privacy_profiles",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("legal_name", sa.String(255)),
        sa.Column("officer_name", sa.String(255)),
        sa.Column("officer_title", sa.String(80), nullable=False),
        sa.Column("officer_email", sa.String(255)),
        sa.Column("officer_phone", sa.String(32)),
        sa.Column("address", sa.Text()),
        sa.Column("purposes", postgresql.JSONB(), nullable=False),
        sa.Column("retention_summary", sa.Text(), nullable=False),
        sa.Column("extra_notice", sa.Text()),
        sa.Column("notice_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("call_disclosure_enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("call_disclosure_text", sa.String(300), nullable=False),
        sa.Column("request_sla_days", sa.Integer(), server_default="30", nullable=False),
        *_ts(),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", name="uq_privacy_profile_tenant"),
    )
    op.create_index("ix_privacy_profiles_tenant_id", "privacy_profiles", ["tenant_id"])

    op.create_table(
        "consent_records",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("customer_id", sa.UUID()),
        sa.Column("subject_name", sa.String(255)),
        sa.Column("subject_phone", sa.String(32)),
        sa.Column("subject_email", sa.String(255)),
        sa.Column("purpose", sa.String(24), nullable=False),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("source", sa.String(24), nullable=False),
        sa.Column("notice_version", sa.Integer()),
        sa.Column("notice_hash", sa.String(64)),
        sa.Column("ip_address", sa.String(45)),
        sa.Column("user_agent", sa.String(255)),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True)),
        sa.Column("withdrawn_via", sa.String(24)),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_consent_records_tenant_id", "consent_records", ["tenant_id"])
    op.create_index("ix_consent_records_customer_id", "consent_records", ["customer_id"])
    op.create_index("ix_consent_tenant_phone", "consent_records", ["tenant_id", "subject_phone"])
    op.create_index("ix_consent_tenant_email", "consent_records", ["tenant_id", "subject_email"])
    op.create_index("ix_consent_tenant_status", "consent_records", ["tenant_id", "status"])

    op.create_table(
        "privacy_requests",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("reference", sa.String(16), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("subject_name", sa.String(255), nullable=False),
        sa.Column("subject_email", sa.String(255)),
        sa.Column("subject_phone", sa.String(32)),
        sa.Column("details", sa.Text()),
        sa.Column("customer_id", sa.UUID()),
        sa.Column("verified_at", sa.DateTime(timezone=True)),
        sa.Column("verified_by_user_id", sa.UUID()),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("resolution", sa.Text()),
        sa.Column("handled_by_user_id", sa.UUID()),
        sa.Column("ip_address", sa.String(45)),
        *_ts(),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["verified_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["handled_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("reference"),
    )
    op.create_index("ix_privacy_requests_tenant_id", "privacy_requests", ["tenant_id"])
    op.create_index(
        "ix_privacy_requests_tenant_status_due", "privacy_requests", ["tenant_id", "status", "due_at"]
    )

    op.create_table(
        "data_breaches",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("discovered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True)),
        sa.Column("data_categories", postgresql.JSONB(), nullable=False),
        sa.Column("approx_people", sa.Integer()),
        sa.Column("likely_impact", sa.Text()),
        sa.Column("mitigation", sa.Text()),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("report_due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("board_notified_at", sa.DateTime(timezone=True)),
        sa.Column("board_reference", sa.String(120)),
        sa.Column("principals_notified_at", sa.DateTime(timezone=True)),
        sa.Column("principals_notified_how", sa.Text()),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.Column("created_by_user_id", sa.UUID()),
        *_ts(),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_data_breaches_tenant_id", "data_breaches", ["tenant_id"])
    op.create_index("ix_data_breaches_tenant_status", "data_breaches", ["tenant_id", "status"])

    op.add_column("retention_policies", sa.Column("lead_days", sa.Integer()))

    conn = op.get_bind()
    for table in TABLES:
        _enable(conn, table, "tenant_id", nullable_rows_readable=False)


def downgrade() -> None:
    conn = op.get_bind()
    op.drop_column("retention_policies", "lead_days")
    for table in reversed(TABLES):
        _disable(conn, table)
        op.drop_table(table)
