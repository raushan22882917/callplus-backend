import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base
from packages.database.mixins import TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin


class QAScorecard(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """Tenant-defined QA criteria a call is scored against. Phrase lists are
    matched case-insensitively against the agent's turns."""

    __tablename__ = "qa_scorecards"

    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500))
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Any one of these in an early agent turn counts as the required greeting
    # (empty list = built-in greeting words).
    greeting_phrases: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    introduction_required: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Every phrase here must appear somewhere in the agent's turns.
    script_phrases: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    # None of these may appear in the agent's turns.
    forbidden_claims: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    # Any one of these in the last agent turns counts as a closing
    # (empty list = built-in closing words).
    closing_phrases: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    max_silence_seconds: Mapped[int] = mapped_column(Integer, default=20, nullable=False)


class CallQAScore(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    __tablename__ = "call_qa_scores"

    call_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calls.id", ondelete="CASCADE"), nullable=False, index=True
    )
    scorecard_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("qa_scorecards.id", ondelete="SET NULL")
    )
    compliance: Mapped[int | None] = mapped_column(Integer)
    communication: Mapped[int | None] = mapped_column(Integer)
    resolution: Mapped[int | None] = mapped_column(Integer)
    sales: Mapped[int | None] = mapped_column(Integer)  # null when the call had no sales angle
    overall: Mapped[int] = mapped_column(Integer, nullable=False)
    # [{"code", "severity": critical|major|minor, "message", "source": rule|llm, "evidence"}]
    problems: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    llm_used: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    scored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("tenant_id", "call_id", name="uq_call_qa_scores_tenant_call"),
        Index("ix_call_qa_scores_tenant_overall", "tenant_id", "overall"),
    )
