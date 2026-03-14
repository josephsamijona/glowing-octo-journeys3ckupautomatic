"""AWS S3 upload service with streaming multipart upload and Redis progress tracking."""
import threading
from datetime import datetime, timezone
from typing import IO

import boto3
import redis
from boto3.s3.transfer import TransferConfig

from app.core.config import get_settings

settings = get_settings()

# Multipart upload: 8 MB chunks, 4 concurrent threads
_TRANSFER_CONFIG = TransferConfig(
    multipart_threshold=8 * 1024 * 1024,
    multipart_chunksize=8 * 1024 * 1024,
    max_concurrency=4,
    use_threads=True,
)


def _s3():
    return boto3.client(
        "s3",
        region_name=settings.aws_region,
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
    )


def _redis():
    return redis.from_url(settings.redis_url, decode_responses=True)


class _ProgressCallback:
    """Called by boto3 for each chunk uploaded; updates Redis progress."""

    def __init__(self, task_id: str, total_bytes: int = 0):
        self._task_id = task_id
        self._total = total_bytes
        self._uploaded = 0
        self._lock = threading.Lock()
        self._redis = _redis()

    def __call__(self, bytes_amount: int):
        with self._lock:
            self._uploaded += bytes_amount
            if self._total > 0:
                pct = int((self._uploaded / self._total) * 60)  # 0-60 %
                progress = 30 + pct  # upload phase: 30 → 90
            else:
                # Unknown size: estimate from MB uploaded (capped at 89)
                mb = self._uploaded / (1024 * 1024)
                progress = min(89, 30 + int(mb * 2))

            try:
                self._redis.set(f"backup:progress:{self._task_id}", progress, ex=3600)
                self._redis.set(
                    f"backup:phase:{self._task_id}", "Upload vers S3...", ex=3600
                )
            except Exception:
                pass  # never block the upload thread on a Redis failure


def upload_stream_to_s3(stream: IO[bytes], task_id: str, filename: str) -> str:
    """
    Stream `stream` directly to S3 via multipart upload.
    Returns the public S3 object URL.
    """
    date_prefix = datetime.now(timezone.utc).strftime("%Y/%m/%d")
    key = f"backups/{date_prefix}/{filename}"

    # Try to learn total size for accurate progress; falls back to 0 (unknown)
    total_bytes = 0
    try:
        pos = stream.seek(0, 2)
        total_bytes = pos
        stream.seek(0)
    except Exception:
        pass

    callback = _ProgressCallback(task_id=task_id, total_bytes=total_bytes)

    _s3().upload_fileobj(
        stream,
        settings.s3_bucket_name,
        key,
        ExtraArgs={"ContentType": "application/gzip"},
        Config=_TRANSFER_CONFIG,
        Callback=callback,
    )

    return (
        f"https://{settings.s3_bucket_name}.s3."
        f"{settings.aws_region}.amazonaws.com/{key}"
    )


def generate_presigned_url(s3_url: str, expiry_seconds: int = 3600) -> str:
    """Generate a time-limited download URL for a backup file."""
    marker = (
        f"{settings.s3_bucket_name}.s3.{settings.aws_region}.amazonaws.com/"
    )
    key = s3_url.split(marker, 1)[-1] if marker in s3_url else s3_url
    return _s3().generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.s3_bucket_name, "Key": key},
        ExpiresIn=expiry_seconds,
    )


def get_bucket_stats() -> dict:
    """Return total bytes and file count for the backups/ prefix."""
    try:
        paginator = _s3().get_paginator("list_objects_v2")
        pages = paginator.paginate(
            Bucket=settings.s3_bucket_name, Prefix="backups/"
        )
        total_size = 0
        file_count = 0
        for page in pages:
            for obj in page.get("Contents", []):
                total_size += obj["Size"]
                file_count += 1
        return {"total_size_bytes": total_size, "file_count": file_count}
    except Exception:
        return {"total_size_bytes": 0, "file_count": 0}
