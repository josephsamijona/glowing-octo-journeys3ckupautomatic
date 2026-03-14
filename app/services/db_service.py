"""DynamoDB CRUD for BackupTasks tracking."""
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import boto3
from boto3.dynamodb.conditions import Attr

from app.core.config import get_settings

settings = get_settings()


def _get_table():
    dynamodb = boto3.resource(
        "dynamodb",
        region_name=settings.aws_region,
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
    )
    return dynamodb.Table(settings.dynamodbtable)


def _mask_db_url(db_url: str) -> str:
    """Replace password in a DB URL with '***'."""
    try:
        parsed = urlparse(db_url)
        if parsed.password:
            masked = db_url.replace(f":{parsed.password}@", ":***@")
            return masked
    except Exception:
        pass
    return "***"


# ---------------------------------------------------------------------------
# Write operations
# ---------------------------------------------------------------------------

def create_task(task_id: str, triggered_by: str, db_url: str = "") -> dict:
    table = _get_table()
    item = {
        "task_id": task_id,
        "status": "PENDING",
        "progress": 0,
        "s3_url": "",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "triggered_by": triggered_by,
        "db_url_masked": _mask_db_url(db_url or settings.db_url),
        "phase": "Initialisation...",
        "file_size": 0,
        "duration_seconds": 0,
        "error_message": "",
    }
    table.put_item(Item=item)
    return item


def update_task(task_id: str, **fields) -> dict:
    """Partial update a task by its task_id."""
    if not fields:
        return {}

    table = _get_table()
    set_parts = []
    expr_names: dict = {}
    expr_values: dict = {}

    for k, v in fields.items():
        placeholder = f"#f_{k}"
        value_key = f":v_{k}"
        set_parts.append(f"{placeholder} = {value_key}")
        expr_names[placeholder] = k
        expr_values[value_key] = v

    response = table.update_item(
        Key={"task_id": task_id},
        UpdateExpression="SET " + ", ".join(set_parts),
        ExpressionAttributeNames=expr_names,
        ExpressionAttributeValues=expr_values,
        ReturnValues="ALL_NEW",
    )
    return response.get("Attributes", {})


# ---------------------------------------------------------------------------
# Read operations
# ---------------------------------------------------------------------------

def get_task(task_id: str) -> Optional[dict]:
    table = _get_table()
    response = table.get_item(Key={"task_id": task_id})
    return response.get("Item")


def list_tasks(limit: int = 50) -> list[dict]:
    """Scan the table, return the most recent `limit` tasks sorted desc."""
    table = _get_table()
    response = table.scan()
    items: list[dict] = response.get("Items", [])

    # Handle pagination
    while "LastEvaluatedKey" in response and len(items) < 200:
        response = table.scan(ExclusiveStartKey=response["LastEvaluatedKey"])
        items.extend(response.get("Items", []))

    items.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    return items[:limit]


def get_last_successful_task() -> Optional[dict]:
    """Return the most recent COMPLETED task."""
    tasks = list_tasks(limit=100)
    return next((t for t in tasks if t.get("status") == "COMPLETED"), None)
