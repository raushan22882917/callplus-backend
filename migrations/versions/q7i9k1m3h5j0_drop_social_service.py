"""Drop Instagram and Messenger from the wallet service default.

Revision ID: q7i9k1m3h5j0
Revises: p6h8j0l2g4i9
Create Date: 2026-11-11 13:20:00.000000

"""
from typing import Sequence, Union

from alembic import op

revision: str = "q7i9k1m3h5j0"
down_revision: Union[str, None] = "p6h8j0l2g4i9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NEW = """{"calls": true, "whatsapp": true, "sms": true, "email": true}"""
_OLD = """{"calls": true, "whatsapp": true, "sms": true, "email": true, "social": true}"""


def upgrade() -> None:
    op.execute(f"ALTER TABLE wallets ALTER COLUMN services SET DEFAULT '{_NEW}'::jsonb")


def downgrade() -> None:
    op.execute(f"ALTER TABLE wallets ALTER COLUMN services SET DEFAULT '{_OLD}'::jsonb")
