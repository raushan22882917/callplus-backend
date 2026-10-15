import uuid

from sqlalchemy import Boolean, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base
from packages.database.mixins import TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin


class AgentTestCase(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """A saved Flow Studio test scenario: a simulated caller (persona) plus checks
    the conversation must pass. See app/services/scenario_runner.py."""

    __tablename__ = "agent_test_cases"

    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ai_agents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # How the simulated caller behaves, in plain words.
    persona: Mapped[str] = mapped_column(String(4000), nullable=False)
    # Optional exact first thing the caller says (otherwise the persona decides).
    opening: Mapped[str] = mapped_column(String(1000), default="", nullable=False)
    # Caller variables for simulated tools: caller_name, caller_phone, customer{}, tickets[].
    variables: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    # [{"type": "must_say"|"must_not_say"|"must_call_tool"|"must_not_call_tool"|"must_reach_node"|"must_end"|"judge", "value": "..."}]
    checks: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    max_turns: Mapped[int] = mapped_column(Integer, default=8, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class AgentTestRun(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """One execution of a test case against a specific flow (saved version or unsaved canvas)."""

    __tablename__ = "agent_test_runs"

    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ai_agents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    case_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_test_cases.id", ondelete="SET NULL"), index=True
    )
    case_name: Mapped[str] = mapped_column(String(200), nullable=False)
    # Groups the runs started by one "Run all" click.
    batch_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    flow_version: Mapped[int | None] = mapped_column(Integer)  # None = unsaved canvas
    status: Mapped[str] = mapped_column(String(20), default="queued", nullable=False)  # queued|running|passed|failed|error
    passed: Mapped[bool | None] = mapped_column(Boolean)
    # [{"check": {...}, "passed": bool, "detail": "..."}]
    results: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    # [{"role", "content", "node_id", "events", "latency_ms"}]
    transcript: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(String(2000))
