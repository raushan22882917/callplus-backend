import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base
from packages.database.mixins import TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin


class WorkflowTrigger(str, enum.Enum):
    CALL_ENDED = "call_ended"
    TICKET_CREATED = "ticket_created"
    CUSTOMER_CREATED = "customer_created"
    MESSAGE_RECEIVED = "message_received"


class WorkflowRunStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class Workflow(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """`definition` is a small JSON DAG: a list of nodes (trigger/condition/
    action) and edges. See app/workflows/engine.py for the node schema and
    the interpreter that walks it. Kept as JSON rather than normalized
    workflow_nodes/workflow_edges tables — there's no need for the DB to
    query into individual nodes, only the engine does, so a JSON blob is the
    simpler correct choice here."""

    __tablename__ = "workflows"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    trigger: Mapped[WorkflowTrigger] = mapped_column(
        Enum(WorkflowTrigger, name="workflow_trigger"), nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    definition: Mapped[dict] = mapped_column(JSONB, nullable=False)

    __table_args__ = (Index("ix_workflows_tenant_trigger", "tenant_id", "trigger"),)


class WorkflowRun(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    __tablename__ = "workflow_runs"

    workflow_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workflows.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    trigger_payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    status: Mapped[WorkflowRunStatus] = mapped_column(
        Enum(WorkflowRunStatus, name="workflow_run_status"),
        default=WorkflowRunStatus.PENDING,
        nullable=False,
    )
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WorkflowTask(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """One executed node within a WorkflowRun — kept for observability
    ("why did this workflow do X"), not for resuming a partially-run
    workflow (runs are re-executed from scratch on retry, not resumed)."""

    __tablename__ = "workflow_tasks"

    workflow_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workflow_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    node_key: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[WorkflowRunStatus] = mapped_column(
        Enum(WorkflowRunStatus, name="workflow_task_status"),
        default=WorkflowRunStatus.PENDING,
        nullable=False,
    )
    result: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
