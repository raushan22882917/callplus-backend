import logging
import threading
import time

from app.core.config import get_settings
from celery import Celery, Task

logger = logging.getLogger(__name__)
settings = get_settings()


class _PublishBreaker:
    """Stops a dead broker from stalling requests.

    Publishing a task from an API handler is a blocking call. With Redis unreachable, Celery's
    defaults retry for ~19 seconds per task (measured), and a handler can publish several, which
    freezes every live call on that API process. After one failure we skip publishing for a short
    while, so an outage costs one short stall instead of one per message."""

    COOL_OFF_SECONDS = 20.0

    def __init__(self) -> None:
        self._open_until = 0.0
        self._lock = threading.Lock()

    def is_open(self) -> bool:
        return time.monotonic() < self._open_until

    def trip(self) -> None:
        with self._lock:
            self._open_until = time.monotonic() + self.COOL_OFF_SECONDS

    def reset(self) -> None:
        self._open_until = 0.0


publish_breaker = _PublishBreaker()


class BrokerUnavailable(RuntimeError):
    """The task queue is down; the work was not queued."""


class FailFastTask(Task):
    def apply_async(self, args=None, kwargs=None, **options):
        if publish_breaker.is_open():
            raise BrokerUnavailable("task queue is unavailable; skipping publish")
        try:
            return super().apply_async(args, kwargs, **options)
        except Exception as exc:
            publish_breaker.trip()
            logger.error("could not publish task %s: %s", self.name, exc)
            raise BrokerUnavailable(str(exc)) from exc


celery_app = Celery(
    "callcenter", broker=settings.redis_url, backend=settings.redis_url, task_cls=FailFastTask
)
celery_app.conf.update(
    task_default_queue="default",
    task_serializer="json",
    accept_content=["json"],
    # Publishing: give up quickly (about a second) instead of the defaults' ~19s of retries.
    broker_connection_timeout=1.0,
    broker_transport_options={"socket_connect_timeout": 1, "socket_timeout": 5, "max_retries": 1},
    task_publish_retry=True,
    task_publish_retry_policy={"max_retries": 1, "interval_start": 0, "interval_step": 0.1, "interval_max": 0.2},
    result_backend_transport_options={"socket_connect_timeout": 1, "socket_timeout": 5, "retry_policy": {"max_retries": 1}},
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
    "campaigns-start-due": {
        "task": "campaigns.start_due",
        "schedule": 60.0,
    },
    "calls-reap-stuck": {
        "task": "calls.reap_stuck_calls",
        "schedule": 2 * 60.0,
    },
    "operations-no-show-followups": {
        "task": "operations.no_show_followups",
        "schedule": 10 * 60.0,
    },
}

# Import modules that register @celery_app.task so `celery -A app.worker.celery_app worker`
# finds them.
from app.worker import (  # noqa: E402,F401
    call_tasks,
    campaign_tasks,
    knowledge_tasks,
    operations_tasks,
    platform_tasks,
    tasks,
    wallet_tasks,
    webhook_tasks,
)
