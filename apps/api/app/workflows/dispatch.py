import uuid
from datetime import UTC, datetime
from typing import Any

from app.workflows.engine import WorkflowExecutionError, run_definition
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import (
    Workflow,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowTask,
    WorkflowTrigger,
)


async def run_pending_workflows(
    db: AsyncSession, *, tenant_id: uuid.UUID, trigger: WorkflowTrigger, payload: dict[str, Any]
) -> None:
    """The actual DB-touching logic — called from the Celery task
    (app/worker/tasks.py) with its own session, and directly from tests.
    Callers that just want to fire this off from a request handler should
    use `enqueue_workflow_trigger` instead, so a slow workflow action
    (a WhatsApp send, a Razorpay call) never blocks the request/call turn.
    """
    result = await db.execute(
        select(Workflow).where(
            Workflow.tenant_id == tenant_id,
            Workflow.trigger == trigger,
            Workflow.is_active.is_(True),
        )
    )
    workflows = result.scalars().all()

    for workflow in workflows:
        run = WorkflowRun(
            tenant_id=tenant_id,
            workflow_id=workflow.id,
            trigger_payload=payload,
            status=WorkflowRunStatus.RUNNING,
            started_at=datetime.now(UTC),
        )
        db.add(run)
        await db.flush()

        async def on_node_complete(node_key: str, status: str, node_result: dict, run=run) -> None:
            db.add(
                WorkflowTask(
                    tenant_id=tenant_id,
                    workflow_run_id=run.id,
                    node_key=node_key,
                    status=WorkflowRunStatus(status),
                    result=node_result,
                    started_at=datetime.now(UTC),
                    completed_at=datetime.now(UTC),
                )
            )
            await db.flush()

        try:
            await run_definition(
                db,
                tenant_id=tenant_id,
                definition=workflow.definition,
                payload=payload,
                on_node_complete=on_node_complete,
            )
            run.status = WorkflowRunStatus.COMPLETED
        except WorkflowExecutionError as exc:
            run.status = WorkflowRunStatus.FAILED
            run.error = str(exc)
        run.completed_at = datetime.now(UTC)
        await db.flush()

    await db.commit()


def enqueue_workflow_trigger(
    *, tenant_id: uuid.UUID, trigger: WorkflowTrigger, payload: dict[str, Any]
) -> None:
    from app.worker.tasks import run_workflow_trigger_task

    run_workflow_trigger_task.delay(str(tenant_id), trigger.value, payload)
