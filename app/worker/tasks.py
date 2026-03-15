"""Celery tasks for the backup pipeline."""
import logging
import time
import uuid
from datetime import datetime, timezone

import redis

from app.core.config import get_settings
from app.services import backup_engine, db_service, s3_service, ses_service
from app.worker.celery_app import celery_app

settings = get_settings()
log = logging.getLogger(__name__)

# Redis key that acts as a global mutex — only one backup runs at a time.
# TTL matches the hard task time limit so the lock always expires even if
# the worker is killed without running the finally block.
_LOCK_KEY = "backup:global_lock"
_LOCK_TTL = 3600   # seconds — same as task_time_limit in celery_app.py


def _redis():
    return redis.from_url(settings.redis_url, decode_responses=True)


def _set_progress(r: redis.Redis, task_id: str, progress: int, phase: str):
    r.set(f"backup:progress:{task_id}", progress, ex=3600)
    r.set(f"backup:phase:{task_id}", phase, ex=3600)


# ---------------------------------------------------------------------------
# Main backup task
# ---------------------------------------------------------------------------

@celery_app.task(bind=True, name="run_backup_process")
def run_backup_process(
    self,
    task_id: str | None = None,
    db_url: str | None = None,
    triggered_by: str = "SYSTEM",
):
    """
    Full backup pipeline:
      1. Acquire global Redis lock  (skip if another backup is running)
      2. Create DynamoDB entry (PENDING → RUNNING)
      3. Run mysqldump / pg_dump (streaming)
      4. Upload gzip stream to S3 via multipart upload
      5. Update DynamoDB (COMPLETED / FAILED)
      6. Send email report via Resend
      7. Release lock
    """
    task_id = task_id or str(uuid.uuid4())
    db_url  = db_url or settings.db_url
    r       = _redis()
    start   = time.monotonic()

    # ── Global singleton lock — prevent overlapping backups ───────────────────
    # nx=True means SET only if the key does NOT exist (atomic check-and-set).
    # If a backup is already running its lock will still be held, so we skip.
    acquired = r.set(_LOCK_KEY, task_id, nx=True, ex=_LOCK_TTL)
    if not acquired:
        running = r.get(_LOCK_KEY)
        log.warning(
            "[lock] Backup already running (task=%s) — skipping %s triggered by %s",
            running, task_id, triggered_by,
        )
        return {"status": "SKIPPED", "task_id": task_id, "reason": "backup_in_progress"}

    try:
        # Ensure DB record exists
        if not db_service.get_task(task_id):
            db_service.create_task(task_id, triggered_by=triggered_by, db_url=db_url)

        # ── Phase 1: RUNNING ─────────────────────────────────────────────
        db_service.update_task(task_id, status="RUNNING", phase="Initialisation...")
        _set_progress(r, task_id, 5, "Initialisation...")

        # ── Phase 2: Dump ─────────────────────────────────────────────────
        _set_progress(r, task_id, 10, "Calcul du dump...")
        db_service.update_task(task_id, progress=10, phase="Calcul du dump...")

        stream, db_type = backup_engine.get_dump_stream(db_url)
        _set_progress(r, task_id, 30, "Upload vers S3...")
        db_service.update_task(task_id, progress=30, phase="Upload vers S3...")

        # ── Phase 3: S3 upload ────────────────────────────────────────────
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = f"backup_{db_type}_{ts}_{task_id[:8]}.sql.gz"
        s3_url = s3_service.upload_stream_to_s3(stream, task_id, filename)

        # ── Phase 4: Finalisation ─────────────────────────────────────────
        _set_progress(r, task_id, 95, "Finalisation...")
        db_service.update_task(task_id, progress=95, phase="Finalisation...")

        # Retrieve file size from the uploaded object for the report
        file_size = 0
        try:
            stats = s3_service.get_bucket_stats()
            # The exact file size would require a separate HeadObject call,
            # but for simplicity we use what the progress callback recorded.
        except Exception:
            pass

        duration = time.monotonic() - start

        db_service.update_task(
            task_id,
            status="COMPLETED",
            progress=100,
            s3_url=s3_url,
            phase="Termine",
            duration_seconds=round(duration, 1),
        )
        _set_progress(r, task_id, 100, "Termine")

        # ── Phase 5: Email ────────────────────────────────────────────────
        task_data = db_service.get_task(task_id) or {}
        ses_service.send_backup_report(
            to_email=settings.admin_email,
            task_id=task_id,
            status="COMPLETED",
            triggered_by=triggered_by,
            db_masked=task_data.get("db_url_masked", "***"),
            s3_url=s3_url,
            file_size_bytes=int(task_data.get("file_size", 0)),
            duration_seconds=duration,
        )

        return {"status": "COMPLETED", "task_id": task_id, "s3_url": s3_url}

    except Exception as exc:
        duration    = time.monotonic() - start
        error_msg   = str(exc)

        db_service.update_task(
            task_id,
            status="FAILED",
            progress=0,
            phase="Erreur",
            error_message=error_msg[:500],
            duration_seconds=round(duration, 1),
        )
        _set_progress(r, task_id, -1, f"Erreur: {error_msg[:100]}")

        task_data = db_service.get_task(task_id) or {}
        ses_service.send_backup_report(
            to_email=settings.admin_email,
            task_id=task_id,
            status="FAILED",
            triggered_by=triggered_by,
            db_masked=task_data.get("db_url_masked", "***"),
            error_message=error_msg,
            duration_seconds=duration,
        )
        raise

    finally:
        # ── Release lock — only if we are still the owner ─────────────────────
        # Guards against the edge case where the lock TTL expired and another
        # backup acquired it while we were finishing up.
        if r.get(_LOCK_KEY) == task_id:
            r.delete(_LOCK_KEY)
            log.info("[lock] Released global backup lock (task=%s)", task_id)
