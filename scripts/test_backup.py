#!/usr/bin/env python3
"""
test_backup.py — Test direct du pipeline backup (sans Celery)

Lance chaque étape séparément avec logs live détaillés.
Utile pour déboguer avant de passer par le worker.

Usage:
    python scripts/test_backup.py           # test complet
    python scripts/test_backup.py --step env     # teste juste les variables d'env
    python scripts/test_backup.py --step redis   # teste juste Redis
    python scripts/test_backup.py --step dynamo  # teste juste DynamoDB
    python scripts/test_backup.py --step s3      # teste juste S3
    python scripts/test_backup.py --step mysql   # teste juste la connexion MySQL (PyMySQL)
    python scripts/test_backup.py --step email   # teste juste l'email
    python scripts/test_backup.py --step full    # pipeline complet (default)
"""
import argparse
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

# ── Load .env ─────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

# ── Logging — verbose, coloured output ────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s  %(levelname)-8s  %(name)-30s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
# Quiet noisy libraries
logging.getLogger("boto3").setLevel(logging.WARNING)
logging.getLogger("botocore").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("s3transfer").setLevel(logging.WARNING)

log = logging.getLogger("test_backup")

PASS = "✅"
FAIL = "❌"
INFO = "ℹ️ "


def section(title: str):
    print(f"\n{'─'*60}")
    print(f"  {title}")
    print(f"{'─'*60}")


# ═══════════════════════════════════════════════════════════════════════════════
# Step 1 — Environment check
# ═══════════════════════════════════════════════════════════════════════════════
def test_env():
    section("STEP 1 — Environment variables")

    required = {
        "DB_URL":            os.getenv("DB_URL", ""),
        "AWS_ACCESS_KEY_ID": os.getenv("AWS_ACCESS_KEY_ID", ""),
        "AWS_SECRET_ACCESS_KEY": os.getenv("AWS_SECRET_ACCESS_KEY", ""),
        "AWS_REGION":        os.getenv("AWS_REGION", ""),
        "S3_BUCKET_NAME":    os.getenv("S3_BUCKET_NAME", ""),
        "REDIS_URL":         os.getenv("REDIS_URL", ""),
        "DynamoDBtable":     os.getenv("DynamoDBtable") or os.getenv("DYNAMODBTABLE", ""),
        "RESEND_API_KEY":    os.getenv("RESEND_API_KEY", ""),
        "EMAIL_FROM":        os.getenv("EMAIL_FROM", ""),
        "ADMIN_EMAIL":       os.getenv("ADMIN_EMAIL", ""),
    }

    all_ok = True
    for k, v in required.items():
        if v:
            display = v[:30] + "..." if len(v) > 30 else v
            log.info("%s  %-30s = %s", PASS, k, display)
        else:
            log.error("%s  %-30s  MISSING", FAIL, k)
            all_ok = False

    return all_ok


# ═══════════════════════════════════════════════════════════════════════════════
# Step 2 — Redis connectivity
# ═══════════════════════════════════════════════════════════════════════════════
def test_redis():
    section("STEP 2 — Redis connection")
    import redis as redis_lib

    url = os.getenv("REDIS_URL", "")
    log.info("Connecting to Redis: %s", url[:40] + "...")
    try:
        r = redis_lib.from_url(url, decode_responses=True, socket_connect_timeout=5)
        pong = r.ping()
        log.info("%s  Redis ping: %s", PASS, pong)

        r.set("test:backup_script", "ok", ex=30)
        val = r.get("test:backup_script")
        log.info("%s  Redis read/write: %s", PASS, val)
        return True
    except Exception as e:
        log.error("%s  Redis connection failed: %s", FAIL, e)
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# Step 3 — DynamoDB
# ═══════════════════════════════════════════════════════════════════════════════
def test_dynamodb():
    section("STEP 3 — DynamoDB connection")
    import boto3

    region     = os.getenv("AWS_REGION", "us-east-1")
    table_name = os.getenv("DynamoDBtable") or os.getenv("DYNAMODBTABLE", "BackupTasks")
    table_name = table_name.strip("'\"")

    log.info("Region : %s", region)
    log.info("Table  : %s", table_name)

    try:
        dynamodb = boto3.resource(
            "dynamodb",
            region_name=region,
            aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
        )
        table = dynamodb.Table(table_name)
        desc  = table.meta.client.describe_table(TableName=table_name)
        status = desc["Table"]["TableStatus"]
        log.info("%s  Table status: %s", PASS, status)

        test_id = f"test-{uuid.uuid4()}"
        table.put_item(Item={
            "TaskId":  test_id,
            "task_id": test_id,
            "status":  "TEST",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        log.info("%s  PutItem OK (TaskId=%s)", PASS, test_id[:16])

        resp = table.get_item(Key={"TaskId": test_id})
        item = resp.get("Item")
        log.info("%s  GetItem OK: status=%s", PASS, item.get("status") if item else "NOT FOUND")

        table.delete_item(Key={"TaskId": test_id})
        log.info("%s  Cleanup OK", PASS)
        return True
    except Exception as e:
        log.error("%s  DynamoDB error: %s", FAIL, e)
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# Step 4 — S3 bucket
# ═══════════════════════════════════════════════════════════════════════════════
def test_s3():
    section("STEP 4 — S3 bucket access")
    import boto3
    import io

    bucket = os.getenv("S3_BUCKET_NAME", "jhbridge-mysql-backups").strip("'\"")
    region = os.getenv("AWS_REGION", "us-east-1")
    log.info("Bucket: %s  Region: %s", bucket, region)

    try:
        s3 = boto3.client(
            "s3",
            region_name=region,
            aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
        )
        s3.head_bucket(Bucket=bucket)
        log.info("%s  Bucket exists and is accessible", PASS)

        test_key = f"backups/test/{uuid.uuid4()}.txt"
        s3.upload_fileobj(
            io.BytesIO(b"backup test ok"),
            bucket,
            test_key,
            ExtraArgs={"ContentType": "text/plain"},
        )
        log.info("%s  Upload OK: s3://%s/%s", PASS, bucket, test_key)

        s3.delete_object(Bucket=bucket, Key=test_key)
        log.info("%s  Cleanup OK", PASS)
        return True
    except Exception as e:
        log.error("%s  S3 error: %s", FAIL, e)
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# Step 5 — MySQL direct connection (PyMySQL — no binary needed)
# ═══════════════════════════════════════════════════════════════════════════════
def test_mysql():
    section("STEP 5 — MySQL connection (PyMySQL)")
    import pymysql
    from urllib.parse import urlparse

    db_url = os.getenv("DB_URL", "")
    if not db_url:
        log.error("%s  DB_URL missing", FAIL)
        return False

    p = urlparse(db_url)
    host     = p.hostname or "127.0.0.1"
    port     = p.port or 3306
    user     = p.username or ""
    password = p.password or ""
    database = (p.path or "").lstrip("/")

    log.info("Host    : %s:%s", host, port)
    log.info("User    : %s", user)
    log.info("Database: %s", database)

    try:
        conn = pymysql.connect(
            host=host, port=port, user=user, password=password,
            database=database, charset="utf8mb4", connect_timeout=10,
        )
        with conn.cursor() as cur:
            cur.execute("SELECT VERSION()")
            version = cur.fetchone()[0]
            log.info("%s  MySQL version: %s", PASS, version)

            cur.execute("SHOW TABLES")
            tables = [row[0] for row in cur.fetchall()]
            log.info("%s  Tables found: %d — %s", PASS, len(tables), ", ".join(tables[:10]))

        conn.close()
        return True
    except Exception as e:
        log.error("%s  MySQL connection failed: %s", FAIL, e)
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# Step 6 — Email via Resend
# ═══════════════════════════════════════════════════════════════════════════════
def test_email():
    section("STEP 6 — Email (Resend)")
    import resend

    api_key    = os.getenv("RESEND_API_KEY", "")
    email_from = os.getenv("EMAIL_FROM", "")
    admin      = os.getenv("ADMIN_EMAIL", "")

    log.info("From  : %s", email_from)
    log.info("To    : %s", admin)

    if not api_key:
        log.error("%s  RESEND_API_KEY missing", FAIL)
        return False

    resend.api_key = api_key
    try:
        recipients = [e.strip() for e in admin.split(",") if e.strip()]
        resp = resend.Emails.send({
            "from":    email_from,
            "to":      recipients,
            "subject": "[JHBridge] Backup test email",
            "html":    "<p>Test email from <b>test_backup.py</b> — pipeline OK.</p>",
        })
        log.info("%s  Email sent: %s", PASS, resp)
        return True
    except Exception as e:
        log.error("%s  Email failed: %s", FAIL, e)
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# Step 7 — Full backup pipeline (direct, no Celery)
# ═══════════════════════════════════════════════════════════════════════════════
def test_full_backup():
    section("STEP 7 — Full backup pipeline (direct, PyMySQL → S3)")

    db_url  = os.getenv("DB_URL", "")
    task_id = str(uuid.uuid4())
    start   = time.monotonic()

    log.info("Task ID : %s", task_id)
    log.info("DB URL  : %s", db_url[:40] + "..." if len(db_url) > 40 else db_url)

    from app.services import backup_engine, db_service, s3_service, ses_service
    from app.core.config import get_settings
    settings = get_settings()

    # ── Create DynamoDB task ───────────────────────────────────────────────────
    log.info("\n[DynamoDB] Creating task record...")
    try:
        db_service.create_task(task_id, triggered_by="TEST_SCRIPT", db_url=db_url)
        db_service.update_task(task_id, status="RUNNING", phase="Test direct")
        log.info("%s  DynamoDB task created", PASS)
    except Exception as e:
        log.error("%s  DynamoDB failed: %s", FAIL, e)
        return False

    # ── Start dump (PyMySQL — no binary needed) ───────────────────────────────
    log.info("\n[PyMySQL] Starting pure-Python dump stream...")
    try:
        stream, db_type = backup_engine.get_dump_stream(db_url)
        log.info("%s  Dump stream started — db_type=%s", PASS, db_type)
    except Exception as e:
        log.error("%s  Dump failed: %s", FAIL, e)
        db_service.update_task(task_id, status="FAILED", error_message=str(e)[:500])
        return False

    # ── Upload to S3 ──────────────────────────────────────────────────────────
    log.info("\n[S3] Uploading to S3 (streaming gzip)...")
    ts       = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"backup_{db_type}_{ts}_{task_id[:8]}.sql.gz"
    try:
        s3_url = s3_service.upload_stream_to_s3(stream, task_id, filename)
        elapsed = time.monotonic() - start
        log.info("%s  Upload complete in %.1fs", PASS, elapsed)
        log.info("    S3 URL: %s", s3_url)
    except Exception as e:
        log.error("%s  S3 upload failed: %s", FAIL, e)
        db_service.update_task(task_id, status="FAILED", error_message=str(e)[:500])
        return False

    # ── Finalise DynamoDB ─────────────────────────────────────────────────────
    duration = time.monotonic() - start
    db_service.update_task(
        task_id,
        status="COMPLETED",
        progress=100,
        s3_url=s3_url,
        phase="Termine",
        duration_seconds=round(duration, 1),
    )
    log.info("%s  DynamoDB updated — COMPLETED", PASS)

    # ── Send email ────────────────────────────────────────────────────────────
    log.info("\n[Email] Sending report...")
    try:
        ses_service.send_backup_report(
            to_email=settings.admin_email,
            task_id=task_id,
            status="COMPLETED",
            triggered_by="TEST_SCRIPT",
            db_masked="***",
            s3_url=s3_url,
            duration_seconds=duration,
        )
        log.info("%s  Email sent", PASS)
    except Exception as e:
        log.warning("⚠️   Email failed (non-blocking): %s", e)

    section(f"✅  BACKUP COMPLETE in {duration:.1f}s")
    log.info("Task ID : %s", task_id)
    log.info("File    : %s", filename)
    log.info("S3 URL  : %s", s3_url)
    return True


# ═══════════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(description="Test backup pipeline step by step")
    parser.add_argument(
        "--step",
        choices=["env", "redis", "dynamo", "s3", "mysql", "email", "full"],
        default="full",
        help="Which step to test (default: full)",
    )
    args = parser.parse_args()

    print("\n" + "═" * 60)
    print("  JHBridge — Backup Pipeline Test")
    print(f"  Step: {args.step.upper()}")
    print("═" * 60)

    runners = {
        "env":    test_env,
        "redis":  test_redis,
        "dynamo": test_dynamodb,
        "s3":     test_s3,
        "mysql":  test_mysql,
        "email":  test_email,
        "full": lambda: (
            test_env() and
            test_redis() and
            test_dynamodb() and
            test_s3() and
            test_mysql() and
            test_full_backup()
        ),
    }

    ok = runners[args.step]()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
