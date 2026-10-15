import asyncio
import logging
import uuid

from app.worker.celery_app import celery_app
from app.worker.tasks import ensure_engine

from packages.database.session import session_scope

logger = logging.getLogger(__name__)


async def _purge() -> dict[str, int]:
    from app.services.platform_retention import purge_all_tenants

    ensure_engine()
    async with session_scope() as db:
        return await purge_all_tenants(db)


@celery_app.task(name="platform.retention_purge")
def retention_purge_task() -> dict[str, int]:
    """Periodic (beat, daily): enforce every enabled retention policy."""
    return asyncio.run(_purge())


async def _dispatch(tenant_id: str, event: str, data: dict) -> list[dict]:
    from app.services.platform_connectors import dispatch_event

    ensure_engine()
    async with session_scope() as db:
        results = await dispatch_event(db, uuid.UUID(tenant_id), event, data)
        await db.commit()
        return results


@celery_app.task(name="platform.connector_dispatch")
def connector_dispatch_task(tenant_id: str, event: str, data: dict) -> list[dict]:
    return asyncio.run(_dispatch(tenant_id, event, data))
