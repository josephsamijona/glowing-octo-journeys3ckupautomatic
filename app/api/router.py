"""All API routes: backups, status, settings, health, WebSocket."""
import asyncio
import json
import logging
import uuid

import boto3
import redis as redis_lib
from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.api.deps import require_api_key, require_auth
from app.api.schemas import (
    HealthResponse,
    HistoryResponse,
    ScheduleSettings,
    TaskStatusResponse,
    TaskSummary,
    TriggerBackupRequest,
    TriggerBackupResponse,
)
from app.core.config import get_settings
from app.services import db_service, s3_service

settings = get_settings()
log = logging.getLogger(__name__)

router = APIRouter()

_SETTINGS_KEY = "backup:settings"
_SCHEDULE_KEY = "backup:schedule"


def _valid_task_id(task_id: str) -> bool:
    """Return True only if task_id is a valid UUID (prevents Redis key injection)."""
    try:
        uuid.UUID(task_id, version=4)
        return True
    except ValueError:
        return False


def _get_redis() -> redis_lib.Redis:
    return redis_lib.from_url(settings.redis_url, decode_responses=True)


# ---------------------------------------------------------------------------
# Auth — Cognito login proxy (public)
# ---------------------------------------------------------------------------

class _LoginRequest(BaseModel):
    email: str
    password: str


@router.post("/auth/login", tags=["auth"])
def login(body: _LoginRequest):
    """Exchange email/password for Cognito tokens. Sets an httpOnly session cookie."""
    cognito = boto3.client("cognito-idp", region_name=settings.cognito_region)
    try:
        resp = cognito.initiate_auth(
            AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": body.email, "PASSWORD": body.password},
            ClientId=settings.cognito_app_client_id,
        )
        tokens = resp.get("AuthenticationResult", {})
        if not tokens:
            raise HTTPException(status_code=401, detail="Authentification échouée.")

        id_token = tokens["IdToken"]

        # Set httpOnly cookie so the server can guard the dashboard route.
        # SameSite=Lax prevents CSRF while allowing normal navigation.
        response = JSONResponse(content={
            "id_token":      id_token,
            "access_token":  tokens["AccessToken"],
            "refresh_token": tokens.get("RefreshToken", ""),
        })
        response.set_cookie(
            key="session_token",
            value=id_token,
            httponly=True,
            secure=True,       # HTTPS only (Railway always uses HTTPS)
            samesite="lax",
            max_age=3600,      # 1 hour — matches Cognito IdToken expiry
            path="/",
        )
        return response

    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("NotAuthorizedException", "UserNotFoundException"):
            raise HTTPException(status_code=401, detail="Email ou mot de passe incorrect.")
        raise HTTPException(status_code=500, detail=f"Erreur Cognito: {code}")


@router.post("/auth/logout", tags=["auth"])
def logout():
    """Clear the session cookie."""
    response = JSONResponse(content={"ok": True})
    response.delete_cookie(key="session_token", path="/")
    return response


# ---------------------------------------------------------------------------
# Health check (public)
# ---------------------------------------------------------------------------

@router.get("/health", response_model=HealthResponse, tags=["system"])
def health_check():
    result = HealthResponse()

    # Redis — log the real error server-side, return "error" to caller
    try:
        r = _get_redis()
        r.ping()
        result.redis = "ok"
    except Exception as exc:
        log.error("Health: Redis unreachable — %s", exc)
        result.redis = "error"
        result.status = "degraded"

    # DynamoDB + last backup
    try:
        last = db_service.get_last_successful_task()
        result.dynamodb = "ok"
        if last:
            result.last_backup = last.get("timestamp", "")
    except Exception as exc:
        log.error("Health: DynamoDB unreachable — %s", exc)
        result.dynamodb = "error"
        result.status = "degraded"

    # S3
    try:
        stats = s3_service.get_bucket_stats()
        result.s3 = "ok"
        result.total_storage_bytes = stats["total_size_bytes"]
        result.total_files = stats["file_count"]
    except Exception as exc:
        log.error("Health: S3 unreachable — %s", exc)
        result.s3 = "error"
        result.status = "degraded"

    return result


# ---------------------------------------------------------------------------
# Trigger backup  (UI or external)
# ---------------------------------------------------------------------------

@router.post(
    "/backups/run",
    response_model=TriggerBackupResponse,
    tags=["backups"],
    dependencies=[Depends(require_auth)],
)
def trigger_backup(body: TriggerBackupRequest = TriggerBackupRequest()):
    from app.worker.tasks import run_backup_process

    task_id = str(uuid.uuid4())
    db_url = body.db_url or settings.db_url
    db_service.create_task(task_id, triggered_by="MANUAL", db_url=db_url)

    run_backup_process.apply_async(
        kwargs={"task_id": task_id, "db_url": db_url, "triggered_by": "MANUAL"},
    )
    return TriggerBackupResponse(task_id=task_id)


# ---------------------------------------------------------------------------
# External API endpoints (API-key only)
# ---------------------------------------------------------------------------

@router.post(
    "/external/trigger",
    response_model=TriggerBackupResponse,
    tags=["external"],
    dependencies=[Depends(require_api_key)],
)
def external_trigger():
    """
    External services can trigger a backup but cannot override the target
    database — the server-configured DB_URL is always used.
    This prevents callers from pivoting to arbitrary databases.
    """
    from app.worker.tasks import run_backup_process

    task_id = str(uuid.uuid4())
    db_service.create_task(task_id, triggered_by="EXTERNAL_API", db_url=settings.db_url)

    run_backup_process.apply_async(
        kwargs={
            "task_id": task_id,
            "db_url": settings.db_url,
            "triggered_by": "EXTERNAL_API",
        },
    )
    return TriggerBackupResponse(task_id=task_id)


@router.get(
    "/external/status/{task_id}",
    response_model=TaskStatusResponse,
    tags=["external"],
    dependencies=[Depends(require_api_key)],
)
def external_status(task_id: str):
    return _build_status(task_id)


# ---------------------------------------------------------------------------
# Backup status + history  (authenticated)
# ---------------------------------------------------------------------------

@router.get(
    "/backups/status/{task_id}",
    response_model=TaskStatusResponse,
    tags=["backups"],
    dependencies=[Depends(require_auth)],
)
def get_backup_status(task_id: str):
    return _build_status(task_id)


@router.get(
    "/backups/history",
    response_model=HistoryResponse,
    tags=["backups"],
    dependencies=[Depends(require_auth)],
)
def get_history(limit: int = 50):
    tasks = db_service.list_tasks(limit=min(limit, 100))
    return HistoryResponse(
        tasks=[TaskSummary(**t) for t in tasks],
        total=len(tasks),
    )


# ---------------------------------------------------------------------------
# Schedule settings (authenticated)
# ---------------------------------------------------------------------------

@router.get(
    "/settings/schedule",
    response_model=ScheduleSettings,
    tags=["settings"],
    dependencies=[Depends(require_auth)],
)
def get_schedule():
    r = _get_redis()
    raw = r.get(_SCHEDULE_KEY)
    if raw:
        try:
            return ScheduleSettings(**json.loads(raw))
        except Exception:
            pass
    return ScheduleSettings()  # defaults: 09:00 / 21:00


@router.put(
    "/settings/schedule",
    response_model=ScheduleSettings,
    tags=["settings"],
    dependencies=[Depends(require_auth)],
)
def update_schedule(body: ScheduleSettings):
    r = _get_redis()
    r.set(_SCHEDULE_KEY, body.model_dump_json())
    return body


# ---------------------------------------------------------------------------
# Download URL  (authenticated)
# ---------------------------------------------------------------------------

@router.get(
    "/backups/{task_id}/download",
    tags=["backups"],
    dependencies=[Depends(require_auth)],
)
def get_download_url(task_id: str):
    task = db_service.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    s3_url = task.get("s3_url", "")
    if not s3_url:
        raise HTTPException(status_code=404, detail="No backup file available.")
    url = s3_service.generate_presigned_url(s3_url, expiry_seconds=3600)
    return {"download_url": url, "expires_in": 3600}


# ---------------------------------------------------------------------------
# WebSocket: live progress  (no auth — task_id is sufficient entropy)
# ---------------------------------------------------------------------------

@router.websocket("/ws/backup/{task_id}")
async def ws_backup_progress(websocket: WebSocket, task_id: str):
    # Validate UUID format before using it as a Redis key prefix
    if not _valid_task_id(task_id):
        await websocket.close(code=1008, reason="Invalid task ID format.")
        return

    await websocket.accept()
    r = _get_redis()

    try:
        while True:
            progress_raw = r.get(f"backup:progress:{task_id}")
            phase = r.get(f"backup:phase:{task_id}") or "En attente..."

            progress = int(progress_raw) if progress_raw is not None else 0

            await websocket.send_json({
                "task_id": task_id,
                "progress": max(0, progress),
                "phase": phase,
                "done": progress >= 100,
                "error": progress < 0,
            })

            if progress >= 100 or progress < 0:
                break

            await asyncio.sleep(0.75)

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        try:
            await websocket.close()
        except RuntimeError:
            pass  # already closed by client disconnect


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------

def _build_status(task_id: str) -> TaskStatusResponse:
    if not _valid_task_id(task_id):
        raise HTTPException(status_code=400, detail="Invalid task ID format.")
    task = db_service.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found.")

    download_url = ""
    if task.get("s3_url") and task.get("status") == "COMPLETED":
        try:
            download_url = s3_service.generate_presigned_url(
                task["s3_url"], expiry_seconds=3600
            )
        except Exception:
            pass

    return TaskStatusResponse(
        task_id=task_id,
        status=task.get("status", "PENDING"),
        progress=int(task.get("progress", 0)),
        phase=task.get("phase", ""),
        s3_url=task.get("s3_url", ""),
        triggered_by=task.get("triggered_by", ""),
        timestamp=task.get("timestamp", ""),
        duration_seconds=float(task.get("duration_seconds", 0)),
        error_message=task.get("error_message", ""),
        download_url=download_url,
    )
