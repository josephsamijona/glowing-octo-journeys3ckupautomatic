"""Pydantic request/response schemas."""
from typing import Literal, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Backup trigger
# ---------------------------------------------------------------------------

class TriggerBackupRequest(BaseModel):
    db_url: Optional[str] = Field(
        default=None,
        description="Override DB URL (uses server default if omitted).",
        examples=["mysql://user:pass@host:3306/dbname"],
    )


class TriggerBackupResponse(BaseModel):
    task_id: str
    status: str = "PENDING"
    message: str = "Backup task queued."


# ---------------------------------------------------------------------------
# Task status
# ---------------------------------------------------------------------------

class TaskStatusResponse(BaseModel):
    task_id: str
    status: Literal["PENDING", "RUNNING", "COMPLETED", "FAILED"]
    progress: int = Field(ge=0, le=100)
    phase: str = ""
    s3_url: str = ""
    triggered_by: str = ""
    timestamp: str = ""
    duration_seconds: float = 0.0
    error_message: str = ""
    download_url: str = ""  # presigned URL, generated on demand


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

class TaskSummary(BaseModel):
    task_id: str
    status: str
    progress: int = 0
    phase: str = ""
    s3_url: str = ""
    triggered_by: str = ""
    timestamp: str = ""
    duration_seconds: float = 0.0
    db_url_masked: str = ""


class HistoryResponse(BaseModel):
    tasks: list[TaskSummary]
    total: int


# ---------------------------------------------------------------------------
# Settings (schedule)
# ---------------------------------------------------------------------------

class ScheduleSettings(BaseModel):
    morning_hour: int = Field(default=9, ge=0, le=23)
    morning_minute: int = Field(default=0, ge=0, le=59)
    evening_hour: int = Field(default=21, ge=0, le=23)
    evening_minute: int = Field(default=0, ge=0, le=59)


# ---------------------------------------------------------------------------
# System health
# ---------------------------------------------------------------------------

class HealthResponse(BaseModel):
    status: str = "ok"
    redis: str = "unknown"
    dynamodb: str = "unknown"
    s3: str = "unknown"
    last_backup: Optional[str] = None
    total_storage_bytes: int = 0
    total_files: int = 0
