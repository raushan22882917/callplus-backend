import asyncio
import uuid

from app.services.webhooks import MAX_ATTEMPTS, attempt_delivery, create_deliveries
from app.worker.celery_app import celery_app
from app.worker.tasks import ensure_engine
from celery import Task

from packages.database.models import WebhookDelivery, WebhookEndpoint
from packages.database.session import session_scope

# Seconds to wait before attempt N+1: 10s, 1m, 5m, 30m, 2h.
RETRY_BACKOFF_SECONDS = (10, 60, 300, 1800, 7200)


async def _fan_out(tenant_id: str, event: str, data: dict) -> list[str]:
    ensure_engine()
    async with session_scope() as db:
        ids = await create_deliveries(db, tenant_id=uuid.UUID(tenant_id), event=event, data=data)
        await db.commit()
    return [str(i) for i in ids]


async def _deliver(delivery_id: str) -> int | None:
    """Returns seconds until the next retry, or None if finished."""
    ensure_engine()
    async with session_scope() as db:
        delivery = await db.get(WebhookDelivery, uuid.UUID(delivery_id))
        if delivery is None or delivery.status in ("succeeded", "failed"):
            return None
        endpoint = await db.get(WebhookEndpoint, delivery.endpoint_id)
        if endpoint is None or not endpoint.enabled:
            delivery.status = "failed"
            delivery.last_error = "endpoint removed or disabled"
            await db.commit()
            return None
        ok = await attempt_delivery(db, delivery, endpoint)
        await db.commit()
        if ok or delivery.attempts >= MAX_ATTEMPTS:
            return None
        return RETRY_BACKOFF_SECONDS[min(delivery.attempts - 1, len(RETRY_BACKOFF_SECONDS) - 1)]


@celery_app.task(name="webhooks.fan_out")
def fan_out_event_task(tenant_id: str, event: str, data: dict) -> None:
    for delivery_id in asyncio.run(_fan_out(tenant_id, event, data)):
        deliver_webhook_task.delay(delivery_id)
    # Third-party connectors (Slack, CRMs, ...) subscribe to the same events.
    from app.worker.platform_tasks import connector_dispatch_task

    connector_dispatch_task.delay(tenant_id, event, data)


@celery_app.task(name="webhooks.deliver", bind=True)
def deliver_webhook_task(self: Task, delivery_id: str) -> None:
    retry_in = asyncio.run(_deliver(delivery_id))
    if retry_in is not None:
        deliver_webhook_task.apply_async(args=[delivery_id], countdown=retry_in)
