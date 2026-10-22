"""Website sources for the knowledge base: running a crawl, keeping its pages in
step with the site, and finding sources that are due for another pass."""

import logging
import uuid
from datetime import UTC, datetime, timedelta

from app.services import crawler, plans
from app.services.knowledge import index_document
from app.services.safe_fetch import FetchError, UnsafeURL
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import KnowledgeDocument, KnowledgeDocumentSource, KnowledgeSource
from packages.database.session import session_scope

logger = logging.getLogger(__name__)

RESYNC_EVERY = timedelta(days=7)
STUCK_AFTER = timedelta(minutes=30)  # a crawl that has not finished by then died with its worker
QUEUED_GRACE = timedelta(minutes=5)  # time for the queue to pick a new source up


def enqueue_crawl(source_id: uuid.UUID) -> None:
    """Hands the crawl to a worker. Never raises: a source left `queued` is found
    again by the periodic sweep."""
    try:
        from app.worker.knowledge_tasks import crawl_source_task

        crawl_source_task.delay(str(source_id))
    except Exception:
        logger.warning("could not enqueue crawl for source %s", source_id, exc_info=True)


async def _set_progress(source_id: uuid.UUID, found: int, done: int) -> None:
    async with session_scope() as db:
        source = await db.get(KnowledgeSource, source_id)
        if source is not None:
            source.pages_found, source.pages_done = found, done
            await db.commit()


async def run_crawl(source_id: uuid.UUID, *, fetch=None, delay: float = 0.3) -> None:
    """Crawls the site and brings the source's documents in line with it: new pages are
    added, changed pages re-indexed, pages that vanished removed. A failed crawl leaves the
    existing documents alone and records why."""
    async with session_scope() as db:
        source = await db.get(KnowledgeSource, source_id)
        if source is None or source.status == "crawling" and _fresh(source):
            return
        tenant_id, url, limit = source.tenant_id, source.url, source.page_limit
        source.status, source.error = "crawling", None
        source.pages_found = source.pages_done = 0
        source.started_at = datetime.now(UTC)
        await db.commit()

    kwargs = {"fetch": fetch} if fetch else {}

    async def progress(found: int, done: int) -> None:
        await _set_progress(source_id, found, done)

    try:
        result = await crawler.crawl(
            url, page_limit=limit, on_progress=progress, delay=delay, **kwargs
        )
    except (FetchError, UnsafeURL, ValueError) as exc:
        await _finish(source_id, "failed", error=str(exc)[:500])
        return
    except Exception as exc:  # a bug must not leave the source "crawling" forever
        logger.exception("crawl of source %s failed", source_id)
        await _finish(source_id, "failed", error=f"Unexpected error: {type(exc).__name__}")
        return
    if not result.pages:
        reasons = ", ".join(f"{n} {why}" for why, n in sorted(result.skipped.items())) or "none found"
        await _finish(source_id, "failed", error=f"No readable pages were found ({reasons})")
        return

    async with session_scope() as db:
        source = await db.get(KnowledgeSource, source_id)
        if source is None:  # deleted while crawling
            return
        existing = {
            d.url: d
            for d in (
                await db.execute(
                    select(KnowledgeDocument).where(KnowledgeDocument.source_id == source_id)
                )
            ).scalars()
        }
        room = await plans.remaining(db, tenant_id, "knowledge_documents")
        added = changed = 0
        skipped_for_plan = 0
        for page in result.pages:
            doc = existing.pop(page.url, None)
            if doc is None:
                if room is not None and room <= 0:
                    skipped_for_plan += 1
                    continue
                doc = KnowledgeDocument(
                    tenant_id=tenant_id,
                    source_id=source_id,
                    url=page.url,
                    title=page.title[:255],
                    content=page.text,
                    source=KnowledgeDocumentSource.TEXT,
                )
                db.add(doc)
                await db.flush()
                await index_document(db, doc)
                added += 1
                if room is not None:
                    room -= 1
            elif doc.content != page.text or doc.title != page.title[:255]:
                doc.title, doc.content = page.title[:255], page.text
                await index_document(db, doc)
                changed += 1
        gone = 0
        for doc in existing.values():  # in the source before, not on the site now
            await db.execute(delete(KnowledgeDocument).where(KnowledgeDocument.id == doc.id))
            gone += 1
        source.status, source.error = "ready", None
        source.last_synced_at = datetime.now(UTC)
        source.pages_done = len(result.pages)
        if skipped_for_plan:
            source.error = (
                f"{skipped_for_plan} page(s) were not added because your plan's document limit was reached"
            )
        await db.commit()
    logger.info("crawl %s: %d added, %d changed, %d removed", source_id, added, changed, gone)


def _fresh(source: KnowledgeSource) -> bool:
    return bool(source.started_at and datetime.now(UTC) - source.started_at < STUCK_AFTER)


async def _finish(source_id: uuid.UUID, status: str, *, error: str | None) -> None:
    async with session_scope() as db:
        source = await db.get(KnowledgeSource, source_id)
        if source is not None:
            source.status, source.error = status, error
            await db.commit()


async def due_sources(db: AsyncSession, now: datetime | None = None) -> list[uuid.UUID]:
    """Sources to crawl now: new ones the queue has not picked up, crawls that died,
    and ready sources whose weekly re-sync has come round."""
    now = now or datetime.now(UTC)
    rows = (await db.execute(select(KnowledgeSource))).scalars().all()
    due = []
    for s in rows:
        age_created = now - s.created_at
        if s.status == "queued" and age_created > QUEUED_GRACE:
            due.append(s.id)
        elif s.status == "crawling" and s.started_at and now - s.started_at > STUCK_AFTER:
            due.append(s.id)
        elif (
            s.status in ("ready", "failed")
            and s.resync_weekly
            and s.last_synced_at is not None
            and now - s.last_synced_at > RESYNC_EVERY
        ):
            due.append(s.id)
    return due
