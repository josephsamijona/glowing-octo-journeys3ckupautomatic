#!/usr/bin/env python3
"""
test_celery_flow.py — End-to-end Celery backup flow test

Étapes:
  1. Vérifie que le worker Celery est joignable via Redis
  2. Déclenche un backup MANUEL via apply_async (comme l'API le fait)
  3. Poll Redis + DynamoDB en temps réel jusqu'à COMPLETED / FAILED
  4. Attend 10 secondes puis planifie un backup SCHEDULÉ (countdown=60s)
  5. Affiche un compte à rebours de 60 secondes
  6. Poll jusqu'à la fin du backup schedulé

Prérequis:
  Terminal 1 (worker):
    .venv\\Scripts\\activate
    celery -A app.worker.celery_app.celery_app worker --pool=solo --loglevel=info --concurrency=1

  Terminal 2 (ce script):
    PYTHONIOENCODING=utf-8 .venv\\Scripts\\python scripts\\test_celery_flow.py
"""
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logging.getLogger("boto3").setLevel(logging.WARNING)
logging.getLogger("botocore").setLevel(logging.WARNING)
logging.getLogger("celery").setLevel(logging.WARNING)
logging.getLogger("kombu").setLevel(logging.WARNING)

log = logging.getLogger("celery_flow_test")

PASS = "[OK]"
FAIL = "[FAIL]"
INFO = "[INFO]"
WAIT = "[WAIT]"


def bar(progress: int, width: int = 40) -> str:
    filled = int(width * max(0, min(100, progress)) / 100)
    return "[" + "#" * filled + "-" * (width - filled) + f"] {progress}%"


def section(title: str):
    print(f"\n{'=' * 65}")
    print(f"  {title}")
    print(f"{'=' * 65}")


# ─────────────────────────────────────────────────────────────────────────────
# Step 0 — verify worker is alive (ping via Celery inspect)
# ─────────────────────────────────────────────────────────────────────────────
def check_worker():
    section("STEP 0 — Worker heartbeat check")
    from app.worker.celery_app import celery_app

    log.info("%s  Pinging Celery workers via Redis broker...", INFO)
    try:
        inspector = celery_app.control.inspect(timeout=5)
        pong = inspector.ping()
        if not pong:
            log.error(
                "%s  No Celery worker responded!\n"
                "       Start one in another terminal:\n"
                "       .venv\\Scripts\\activate\n"
                "       celery -A app.worker.celery_app.celery_app worker "
                "--pool=solo --loglevel=info --concurrency=1",
                FAIL,
            )
            return False

        for worker_name in pong:
            log.info("%s  Worker online: %s", PASS, worker_name)
        return True
    except Exception as e:
        log.error("%s  Broker unreachable: %s", FAIL, e)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Poll helper — reads Redis progress + DynamoDB status
# ─────────────────────────────────────────────────────────────────────────────
def poll_until_done(task_id: str, label: str, timeout: int = 600) -> bool:
    import redis as redis_lib
    from app.services import db_service

    r = redis_lib.from_url(os.getenv("REDIS_URL", ""), decode_responses=True)
    deadline = time.monotonic() + timeout
    last_phase = ""
    last_progress = -1

    log.info("%s  Polling task %s ...", WAIT, task_id[:16])

    while time.monotonic() < deadline:
        # Redis gives us live progress (updated by the worker in real-time)
        progress_raw = r.get(f"backup:progress:{task_id}")
        phase = r.get(f"backup:phase:{task_id}") or "En attente du worker..."
        progress = int(progress_raw) if progress_raw is not None else 0

        if progress != last_progress or phase != last_phase:
            if progress < 0:
                log.info("  [ERROR] %s  progress=%d", phase, progress)
            else:
                log.info("  %s  %s  phase=%s", bar(progress), label, phase)
            last_progress = progress
            last_phase = phase

        if progress >= 100:
            # Confirm via DynamoDB (source of truth)
            task = db_service.get_task(task_id)
            status = task.get("status", "?") if task else "NOT FOUND"
            s3_url = task.get("s3_url", "") if task else ""
            duration = float(task.get("duration_seconds", 0)) if task else 0

            log.info("")
            log.info("%s  COMPLETED in %.1fs", PASS, duration)
            log.info("     DynamoDB status : %s", status)
            if s3_url:
                log.info("     S3 URL          : %s", s3_url)
            return status == "COMPLETED"

        if progress < 0:
            task = db_service.get_task(task_id)
            err = task.get("error_message", "unknown") if task else "no record"
            log.error("%s  FAILED — %s", FAIL, err)
            return False

        time.sleep(2)

    log.error("%s  Timeout after %ds — task may still be running", FAIL, timeout)
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Step 1 — Manual backup via Celery
# ─────────────────────────────────────────────────────────────────────────────
def run_manual_backup() -> tuple[bool, str]:
    section("STEP 1 — Manual backup (Celery apply_async)")
    from app.services import db_service
    from app.worker.tasks import run_backup_process
    from app.core.config import get_settings

    settings = get_settings()
    task_id = str(uuid.uuid4())
    db_url = settings.db_url

    log.info("%s  Task ID  : %s", INFO, task_id)
    log.info("%s  DB URL   : %s...", INFO, db_url[:40])
    log.info("%s  Triggered: MANUAL_TEST", INFO)

    # Create DynamoDB record so the worker can update it
    db_service.create_task(task_id, triggered_by="MANUAL_TEST", db_url=db_url)

    # Fire the Celery task (same call the API makes)
    run_backup_process.apply_async(
        kwargs={
            "task_id": task_id,
            "db_url": db_url,
            "triggered_by": "MANUAL_TEST",
        }
    )

    log.info("%s  Task sent to Celery broker. Waiting for worker to pick it up...", PASS)

    ok = poll_until_done(task_id, label="MANUAL")
    return ok, task_id


# ─────────────────────────────────────────────────────────────────────────────
# Step 2 — Scheduled backup (simulated with countdown=60s)
# ─────────────────────────────────────────────────────────────────────────────
def run_scheduled_backup() -> tuple[bool, str]:
    section("STEP 2 — Scheduled backup (simulated — fires in 60s)")
    from app.services import db_service
    from app.worker.tasks import run_backup_process
    from app.core.config import get_settings

    settings = get_settings()
    task_id = str(uuid.uuid4())
    db_url = settings.db_url

    log.info("%s  Task ID  : %s", INFO, task_id)
    log.info("%s  Triggered: SCHEDULED_TEST (countdown=60s)", INFO)

    db_service.create_task(task_id, triggered_by="SCHEDULED_TEST", db_url=db_url)

    # countdown=60 tells Celery to hold the message for 60 seconds before
    # delivering it to the worker — exactly what Beat does with crontab.
    run_backup_process.apply_async(
        kwargs={
            "task_id": task_id,
            "db_url": db_url,
            "triggered_by": "SCHEDULED_TEST",
        },
        countdown=60,
    )

    log.info("%s  Task queued with 60s delay. Countdown:", PASS)

    # Live countdown display
    for remaining in range(60, 0, -1):
        print(f"\r  Scheduled backup fires in {remaining:2d}s ...  ", end="", flush=True)
        time.sleep(1)
    print()

    log.info("%s  60s elapsed — worker should now be executing the task", INFO)

    ok = poll_until_done(task_id, label="SCHEDULED")
    return ok, task_id


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
def main():
    print("\n" + "=" * 65)
    print("  JHBridge — Full Celery Backup Flow Test")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("=" * 65)
    print()
    print("  This script tests the COMPLETE Celery pipeline:")
    print("    Step 0  : Verify Celery worker is running")
    print("    Step 1  : Trigger manual backup  (apply_async now)")
    print("    Step 2  : Trigger scheduled backup (countdown=60s)")
    print()
    print("  REQUIREMENT: Start the worker in another terminal FIRST:")
    print("    .venv\\Scripts\\activate")
    print("    celery -A app.worker.celery_app.celery_app worker \\")
    print("           --pool=solo --loglevel=info --concurrency=1")
    print()

    results = {}

    # ── Step 0: worker health ────────────────────────────────────────────────
    if not check_worker():
        sys.exit(1)

    # ── Step 1: manual backup ────────────────────────────────────────────────
    ok1, tid1 = run_manual_backup()
    results["manual"] = {"ok": ok1, "task_id": tid1}

    if not ok1:
        log.error("%s  Manual backup failed — skipping scheduled test", FAIL)
        _print_summary(results)
        sys.exit(1)

    # Brief pause between the two backups (Redis lock needs to be released)
    log.info("")
    log.info("%s  Manual backup done. Waiting 10s before scheduling next...", INFO)
    time.sleep(10)

    # ── Step 2: scheduled backup ─────────────────────────────────────────────
    ok2, tid2 = run_scheduled_backup()
    results["scheduled"] = {"ok": ok2, "task_id": tid2}

    _print_summary(results)
    sys.exit(0 if (ok1 and ok2) else 1)


def _print_summary(results: dict):
    section("SUMMARY")
    for name, r in results.items():
        status = PASS if r["ok"] else FAIL
        log.info("%s  %-12s  task_id=%s", status, name.upper(), r["task_id"][:16] + "...")
    print()


if __name__ == "__main__":
    main()
