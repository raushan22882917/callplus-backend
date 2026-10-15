"""WhatsApp: human takeover pause on conversations, delivery status on messages

Revision ID: f7b9d1e3a5c4
Revises: e6a8c0d2f4b3
Create Date: 2026-10-03 12:40:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f7b9d1e3a5c4"
down_revision: Union[str, None] = "e6a8c0d2f4b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("conversations", sa.Column("ai_paused_until", sa.DateTime(timezone=True), nullable=True))
    op.add_column("messages", sa.Column("delivery_status", sa.String(length=16), nullable=True))
    op.add_column("messages", sa.Column("delivery_error", sa.Text(), nullable=True))
    # Delivery receipts arrive keyed only by the provider's message id.
    op.create_index("ix_messages_provider_message_id", "messages", ["provider_message_id"])
    # Numbers connected before an agent could be chosen were never answered.
    # Give each one its business's agent when the business has exactly one.
    op.execute(
        """
        UPDATE whatsapp_accounts wa SET ai_agent_id = a.id
        FROM ai_agents a
        WHERE wa.ai_agent_id IS NULL AND a.tenant_id = wa.tenant_id AND a.status <> 'DISABLED'
          AND (SELECT count(*) FROM ai_agents x WHERE x.tenant_id = wa.tenant_id AND x.status <> 'DISABLED') = 1
        """
    )


def downgrade() -> None:
    op.drop_index("ix_messages_provider_message_id", table_name="messages")
    op.drop_column("messages", "delivery_error")
    op.drop_column("messages", "delivery_status")
    op.drop_column("conversations", "ai_paused_until")
