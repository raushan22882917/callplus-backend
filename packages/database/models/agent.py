import enum
import uuid

from sqlalchemy import Boolean, Enum, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base
from packages.database.mixins import TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin


class AgentStatus(str, enum.Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    DISABLED = "disabled"


class AIAgent(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    __tablename__ = "ai_agents"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000))
    status: Mapped[AgentStatus] = mapped_column(
        Enum(AgentStatus, name="agent_status"), default=AgentStatus.DRAFT, nullable=False
    )
    language: Mapped[str] = mapped_column(String(10), default="en-IN", nullable=False)
    voice: Mapped[str | None] = mapped_column(String(100))
    greeting: Mapped[str | None] = mapped_column(String(1000))
    # Free-form system prompt / behaviour config for the active version. Full
    # history lives in AIAgentVersion; this is a denormalized "current" copy
    # so the realtime turn loop can read one row instead of joining.
    system_prompt: Mapped[str] = mapped_column(
        String(8000),
        default=(
            "You are a helpful AI assistant for this business. Use the available "
            "tools when they help, and answer any other question the caller has "
            "as best you can."
        ),
        nullable=False,
    )

    # Call-control policy — all owner-configurable per agent (see
    # app/api/v1/internal.py for enforcement: duration is checked on every
    # turn, no-response retries are tracked by the Go gateway using the
    # timeout/limit/prompts resolved here at call start).
    max_call_duration_seconds: Mapped[int] = mapped_column(Integer, default=300, nullable=False)
    no_response_timeout_seconds: Mapped[int] = mapped_column(Integer, default=6, nullable=False)
    max_no_response_retries: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    no_response_prompt: Mapped[str] = mapped_column(
        String(500), default="Sorry, I didn't catch that — could you repeat it?", nullable=False
    )
    max_duration_message: Mapped[str] = mapped_column(
        String(500),
        default="I'm sorry, but I need to end our call now. Thank you for calling.",
        nullable=False,
    )
    no_response_end_message: Mapped[str] = mapped_column(
        String(500),
        default=(
            "I'm still not hearing anything, so I'll end the call here. Feel free to call back."
        ),
        nullable=False,
    )

    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_ai_agent_tenant_name"),)


class AIAgentVersion(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    __tablename__ = "ai_agent_versions"

    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ai_agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    configuration: Mapped[dict] = mapped_column(JSONB, nullable=False)
    published: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    __table_args__ = (UniqueConstraint("agent_id", "version", name="uq_agent_version"),)


class AIAgentTool(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """Which tools (from the code-level tool registry, see app/tools/registry.py)
    an agent is allowed to call, and any per-agent configuration for them.
    The registry — not this table — defines what a tool actually does."""

    __tablename__ = "ai_agent_tools"

    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ai_agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tool_name: Mapped[str] = mapped_column(String(100), nullable=False)
    configuration: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    __table_args__ = (UniqueConstraint("agent_id", "tool_name", name="uq_agent_tool"),)
