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


async def _release_idle_numbers() -> dict:
    from app.services.number_lifecycle import release_idle_platform_numbers

    ensure_engine()
    async with session_scope() as db:
        released = await release_idle_platform_numbers(db)
    return {"released": released}


@celery_app.task(name="calls.reap_stuck_calls")
def reap_stuck_calls_task() -> dict:
    return asyncio.run(_reap())


@celery_app.task(name="calls.release_idle_numbers")
def release_idle_numbers_task() -> dict:
    return asyncio.run(_release_idle_numbers())


async def _recover_missed() -> dict:
    from app.services.callbacks import recover_missed_calls

    ensure_engine()
    async with session_scope() as db:
        recovered = await recover_missed_calls(db)
    return {"called_back": recovered}


@celery_app.task(name="calls.recover_missed")
def recover_missed_calls_task() -> dict:
    return asyncio.run(_recover_missed())
