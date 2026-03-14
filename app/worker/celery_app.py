from celery import Celery
from celery.schedules import crontab

from app.core.config import get_settings

settings = get_settings()

celery_app = Celery(
    "s3_backup_flow",
    broker=settings.celery_broker_url,
    backend=settings.redis_url,
    include=["app.worker.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,

    # ── Resource limits ───────────────────────────────────────────────────────
    # Hard kill the task after 1h — prevents mysqldump from hanging forever
    # and racking up costs (this is what caused the expensive loop before).
    task_time_limit=3600,
    # Soft limit at 55min: sends SIGTERM so the task can clean up gracefully
    # before the hard kill fires 5 minutes later.
    task_soft_time_limit=3300,

    # ── Worker behaviour — one backup at a time ───────────────────────────────
    worker_prefetch_multiplier=1,   # Never pre-fetch: take 1 task, finish it, then take the next
    task_acks_late=True,            # Ack only after the task completes (safe re-queue on crash)
    worker_concurrency=1,           # 1 backup process per worker container
    # Restart the worker process after N tasks to free any memory leaks
    # from mysqldump/pg_dump subprocess handling.
    worker_max_tasks_per_child=20,
    # Kill the worker process if it exceeds 300 MB (guards against dump leaks).
    # Unit is kilobytes.
    worker_max_memory_per_child=307_200,

    # ── Beat scheduler — prevent schedule drift ───────────────────────────────
    # Beat wakes up at most every 5 minutes to check the schedule.
    # This avoids a tight polling loop that burns CPU.
    beat_max_loop_interval=300,

    # ── Beat schedule: 2 backups per day, 09:00 and 21:00 UTC ────────────────
    beat_schedule={
        "morning-backup": {
            "task": "run_backup_process",
            "schedule": crontab(hour=9, minute=0),
            "kwargs": {"triggered_by": "SYSTEM"},
        },
        "evening-backup": {
            "task": "run_backup_process",
            "schedule": crontab(hour=21, minute=0),
            "kwargs": {"triggered_by": "SYSTEM"},
        },
    },
)
