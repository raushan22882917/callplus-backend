"""Periodic call housekeeping (scheduled in app/worker/celery_app.py)."""

import asyncio

from app.services.call_reaper import reap_stuck_calls
from app.worker.celery_app import celery_app
from app.worker.tasks import ensure_engine

from packages.database.session import session_scope


async def _reap() -> dict:
    ensure_engine()
    async with session_scope() as db:
        result = await reap_stuck_calls(db)
    return {"checked": result.checked, "finalized": result.finalized, "hung_up": result.still_live}


@celery_app.task(name="calls.reap_stuck_calls")
def reap_stuck_calls_task() -> dict:
    return asyncio.run(_reap())
