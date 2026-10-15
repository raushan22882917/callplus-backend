import asyncio

import packages.database.session as session_module
from app.worker.tasks import ensure_engine
from sqlalchemy import text


def test_consecutive_worker_tasks_reuse_the_engine_across_event_loops():
    """Celery runs every task in a fresh asyncio.run(); a pooled connection from the first
    task's loop must not be handed to the second ("attached to a different loop")."""
    saved = session_module._engine, session_module._session_factory
    session_module._engine = session_module._session_factory = None
    try:
        ensure_engine()

        async def one_task() -> int:
            async with session_module.session_scope() as db:
                return (await db.execute(text("select 1"))).scalar_one()

        assert [asyncio.run(one_task()) for _ in range(3)] == [1, 1, 1]
    finally:
        session_module._engine, session_module._session_factory = saved
