#!/bin/sh
# entrypoint.sh — Selects which service to run based on SERVICE env var.
# Usage: docker run -e SERVICE=worker image
#
# SERVICE=api     → uvicorn FastAPI server  (default)
# SERVICE=worker  → celery worker
# SERVICE=beat    → celery beat scheduler

set -eu

SERVICE="${SERVICE:-api}"

case "$SERVICE" in
  api)
    echo "[entrypoint] Starting API server on port ${PORT:-8000}..."
    exec uvicorn app.main:app \
      --host 0.0.0.0 \
      --port "${PORT:-8000}" \
      --workers 2 \
      --log-level info
    ;;

  worker)
    echo "[entrypoint] Starting Celery worker..."
    exec celery -A app.worker.celery_app.celery_app worker \
      --loglevel=info \
      --concurrency=1 \
      --max-tasks-per-child=20 \
      --max-memory-per-child=307200
    ;;

  beat)
    echo "[entrypoint] Starting Celery beat scheduler..."
    exec celery -A app.worker.celery_app.celery_app beat \
      --loglevel=info \
      --scheduler celery.beat.PersistentScheduler \
      --schedule /tmp/celerybeat-schedule \
      --max-interval 300
    ;;

  *)
    echo "[entrypoint] ERROR: unknown SERVICE='$SERVICE'. Use api | worker | beat." >&2
    exit 1
    ;;
esac
