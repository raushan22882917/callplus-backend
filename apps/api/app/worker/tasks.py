import asyncio
import uuid

from app.core.config import get_settings
from app.workflows.dispatch import run_pending_workflows

from packages.database.models import WorkflowTrigger
from packages.database.session import get_engine, init_engine, session_scope


def ensure_engine() -> None:
    """Celery worker processes never go through FastAPI's lifespan, so the
    DB engine needs an explicit first-use init — shared by every task
    module (see also app/worker/campaign_tasks.py)."""
    try:
        get_engine()
    except RuntimeError:
        init_engine(get_settings().database_url, null_pool=True)


async def _run_workflow_trigger(tenant_id: str, trigger: str, payload: dict) -> None:
    ensure_engine()
    async with session_scope() as db:
        await run_pending_workflows(
            db,
            tenant_id=uuid.UUID(tenant_id),
            trigger=WorkflowTrigger(trigger),
            payload=payload,
        )


# Imported lazily by app.worker.celery_app after `celery_app` is defined, and
# by app.workflows.dispatch.enqueue_workflow_trigger — both import this
# module, not the other way around, to avoid a circular import at load time.
from app.worker.celery_app import celery_app  # noqa: E402


@celery_app.task(name="workflows.run_trigger")
def run_workflow_trigger_task(tenant_id: str, trigger: str, payload: dict) -> None:
    asyncio.run(_run_workflow_trigger(tenant_id, trigger, payload))


async def _analyze_call(call_id: str) -> None:
    from app.services.call_analysis import analyze_call

    ensure_engine()
    async with session_scope() as db:
        await analyze_call(db, uuid.UUID(call_id))


@celery_app.task(name="calls.analyze", autoretry_for=(Exception,), retry_backoff=True, max_retries=2)
def analyze_call_task(call_id: str) -> None:
    asyncio.run(_analyze_call(call_id))


async def _score_call_qa(call_id: str) -> None:
    from app.services.qa import score_call

    ensure_engine()
    async with session_scope() as db:
        await score_call(db, uuid.UUID(call_id))


@celery_app.task(name="insights.score_call", autoretry_for=(Exception,), retry_backoff=True, max_retries=2)
def score_call_qa_task(call_id: str) -> None:
    asyncio.run(_score_call_qa(call_id))


async def _notify_after_call(call_id: str) -> None:
    from app.services.post_call import notify_after_call

    ensure_engine()
    async with session_scope() as db:
        await notify_after_call(db, uuid.UUID(call_id))


@celery_app.task(name="calls.notify_after", autoretry_for=(Exception,), retry_backoff=True, max_retries=2)
def notify_after_call_task(call_id: str) -> None:
    asyncio.run(_notify_after_call(call_id))
