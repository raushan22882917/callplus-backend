"""Periodic operations jobs (scheduled in app/worker/celery_app.py)."""

import asyncio

from app.services import operations as svc
from app.services.webhooks import enqueue_webhook_event
from app.worker.celery_app import celery_app
from app.worker.tasks import ensure_engine

from packages.database.session import session_scope


async def _check_sla_breaches() -> int:
    ensure_engine()
    async with session_scope() as db:
        breaches = await svc.refresh_sla(db)
        await db.commit()
    for b in breaches:
        enqueue_webhook_event(
            tenant_id=b.tenant_id,
            event="sla.breached",
            data={
                "ticket_id": str(b.ticket_id),
                "breach": b.kind,
                "priority": b.priority,
                "department": b.department,
                "due_at": b.due_at.isoformat(),
            },
        )
    return len(breaches)


async def _reminders() -> dict:
    ensure_engine()
    async with session_scope() as db:
        result = await svc.send_due_reminders(db)
    return {"sent": result.sent, "failed": result.failed}


async def _no_show() -> int:
    ensure_engine()
    async with session_scope() as db:
        return await svc.create_no_show_followups(db)


async def _follow_ups() -> dict:
    ensure_engine()
    async with session_scope() as db:
        return await svc.run_due_follow_ups(db)


@celery_app.task(name="operations.check_sla_breaches")
def check_sla_breaches_task() -> int:
    return asyncio.run(_check_sla_breaches())


@celery_app.task(name="operations.send_appointment_reminders")
def send_appointment_reminders_task() -> dict:
    return asyncio.run(_reminders())


@celery_app.task(name="operations.no_show_followups")
def no_show_followups_task() -> int:
    return asyncio.run(_no_show())


@celery_app.task(name="operations.run_due_follow_ups")
def run_due_follow_ups_task() -> dict:
    return asyncio.run(_follow_ups())


async def _csat() -> dict:
    ensure_engine()
    from app.services import csat

    async with session_scope() as db:
        return await csat.send_due_surveys(db)


@celery_app.task(name="csat.send_due_surveys")
def send_due_csat_surveys() -> dict:
    return asyncio.run(_csat())
