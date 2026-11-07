"""Mark numbers this platform bought, so they can be released

Revision ID: e4b6d8f0a2c7
Revises: d3f5a7b9c1e6
Create Date: 2026-11-07 10:30:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e4b6d8f0a2c7"
down_revision: Union[str, None] = "d3f5a7b9c1e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "phone_numbers",
        sa.Column(
            "platform_owned",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("phone_numbers", "platform_owned")
