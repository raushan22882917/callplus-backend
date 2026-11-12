"""The services a workspace chooses to use.

Revision ID: o5g7i9k1f3h8
Revises: n4f6h8j0e2g7
Create Date: 2026-11-11 11:10:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "o5g7i9k1f3h8"
down_revision: Union[str, None] = "n4f6h8j0e2g7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_DEFAULT = '{"calls": true, "whatsapp": true, "sms": true, "email": true, "social": true}'


def upgrade() -> None:
    op.add_column(
        "wallets",
        sa.Column(
            "services",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=_DEFAULT,
        ),
    )


def downgrade() -> None:
    op.drop_column("wallets", "services")
