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
    # Process one backup at a time to avoid resource contention
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    # Beat schedule: automatic backups at 09:00 and 21:00 UTC
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
