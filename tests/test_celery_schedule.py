from app.worker.celery_app import celery_app


def test_every_scheduled_task_is_registered():
    """A beat entry naming a task that no module registers fails silently at runtime,
    and a module edit can drop entries without any other test noticing."""
    scheduled = {entry["task"] for entry in celery_app.conf.beat_schedule.values()}
    assert {
        "operations.check_sla_breaches",
        "operations.run_due_follow_ups",
        "operations.send_appointment_reminders",
        "operations.no_show_followups",
        "platform.retention_purge",
    } <= scheduled
    missing = scheduled - set(celery_app.tasks)
    assert not missing, f"scheduled but not registered: {sorted(missing)}"
