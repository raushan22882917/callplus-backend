import asyncio
import uuid

from app.worker.celery_app import celery_app
from app.worker.tasks import ensure_engine

from packages.database.session import session_scope


async def _crawl(source_id: str) -> None:
    from app.services.knowledge_sources import run_crawl

    ensure_engine()
    await run_crawl(uuid.UUID(source_id))


@celery_app.task(name="knowledge.crawl_source", time_limit=1800)
def crawl_source_task(source_id: str) -> None:
    asyncio.run(_crawl(source_id))


async def _sweep() -> int:
    from app.services.knowledge_sources import due_sources, enqueue_crawl

    ensure_engine()
    async with session_scope() as db:
        ids = await due_sources(db)
    for source_id in ids:
        enqueue_crawl(source_id)
    return len(ids)


@celery_app.task(name="knowledge.sweep_sources")
def sweep_sources_task() -> int:
    """Periodic: start new sources the queue missed and the weekly re-syncs."""
    return asyncio.run(_sweep())
