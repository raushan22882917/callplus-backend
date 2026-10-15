import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base
from packages.database.mixins import TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin


class GuardrailRule(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """One behavioural rule for the AI agent. `ai_agent_id` NULL means the rule
    applies to every agent of the tenant. `kind` is validated in the API schema
    (see app/schemas/guardrails.py); `params` holds the kind-specific settings
    (keywords, percent, working hours, custom safe message...)."""

    __tablename__ = "guardrail_rules"

    ai_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ai_agents.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    text: Mapped[str | None] = mapped_column(Text)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)


class GuardrailEvent(Base, UUIDPrimaryKeyMixin, TenantScopedMixin):
    """Audit row written whenever the post-check changed or escalated a reply."""

    __tablename__ = "guardrail_events"

    call_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calls.id", ondelete="SET NULL"), index=True
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="SET NULL"), index=True
    )
    rule_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("guardrail_rules.id", ondelete="SET NULL"), index=True
    )
    rule_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    excerpt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_guardrail_events_tenant_created", "tenant_id", "created_at"),)


class KnowledgeAIControl(Base, UUIDPrimaryKeyMixin, TenantScopedMixin):
    """Per-document switch for the AI. No row means enabled + customer-facing.
    visibility: "customer" (may be quoted) or "internal" (background only)."""

    __tablename__ = "knowledge_ai_controls"

    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_documents.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    visibility: Mapped[str] = mapped_column(String(20), default="customer", nullable=False)
