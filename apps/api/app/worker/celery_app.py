from app.core.config import get_settings
from celery import Celery

settings = get_settings()

celery_app = Celery("callcenter", broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.update(
    task_default_queue="default", task_serializer="json", accept_content=["json"]
)

celery_app.conf.beat_schedule = {
    "platform-retention-purge": {
        "task": "platform.retention_purge",
        "schedule": 24 * 60 * 60.0,
    },
    "operations-check-sla-breaches": {
        "task": "operations.check_sla_breaches",
        "schedule": 60.0,
    },
    "operations-run-due-follow-ups": {
        "task": "operations.run_due_follow_ups",
        "schedule": 60.0,
    },
    "operations-send-appointment-reminders": {
        "task": "operations.send_appointment_reminders",
        "schedule": 5 * 60.0,
    },
    "csat-send-due-surveys": {
        "task": "csat.send_due_surveys",
        "schedule": 60.0,
    },
    "wallet-settle": {
        "task": "wallet.settle_all",
        "schedule": 60.0,
    },
    "knowledge-sweep-sources": {
        "task": "knowledge.sweep_sources",
        "schedule": 10 * 60.0,
    },
    "operations-no-show-followups": {
        "task": "operations.no_show_followups",
        "schedule": 10 * 60.0,
    },
}

# Import modules that register @celery_app.task so `celery -A app.worker.celery_app worker`
# finds them.
from app.worker import (  # noqa: E402,F401
    campaign_tasks,
    knowledge_tasks,
    wallet_tasks,
    operations_tasks,
    platform_tasks,
    tasks,
    webhook_tasks,
)
