from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from .models import ActivityEvent, FileRecord, ReportSchedule, Watch
from .time_utils import api_datetime

EVENT_LABELS = {
    "created": "Создан",
    "updated": "Обновлён",
    "renamed": "Переименован",
    "moved": "Перемещён",
    "copied": "Скопирован",
    "deleted": "Удалён",
}


def watch_dict(item: Watch) -> dict[str, Any]:
    return {
        "id": item.id,
        "name": item.name,
        "root_path": item.root_path,
        "owner_user_id": item.owner_user_id,
        "scope_type": item.scope_type,
        "group_ids": item.group_ids or [],
        "started_at": api_datetime(item.started_at),
        "ended_at": api_datetime(item.ended_at),
        "status": item.status.value,
        "created_at": api_datetime(item.created_at),
        "updated_at": api_datetime(item.updated_at),
        "created_by": item.created_by,
        "last_webhook_at": api_datetime(item.last_webhook_at),
    }


def file_state(item: FileRecord) -> str:
    if not item.exists:
        return "deleted"
    if item.revision_count:
        return "updated"
    return "new"


def file_dict(item: FileRecord) -> dict[str, Any]:
    return {
        "id": item.id,
        "watch_id": item.watch_id,
        "nextcloud_file_id": item.nextcloud_file_id,
        "filename": item.filename,
        "full_path": item.full_path,
        "relative_path": item.relative_path,
        "parent_path": item.parent_path,
        "mime_type": item.mime_type,
        "extension": item.extension,
        "size": item.size,
        "etag": item.etag,
        "first_seen_at": api_datetime(item.first_seen_at),
        "last_seen_at": api_datetime(item.last_seen_at),
        "nextcloud_upload_time": api_datetime(item.nextcloud_upload_time),
        "nextcloud_mtime": api_datetime(item.nextcloud_mtime),
        "exists": item.exists,
        "deleted_at": api_datetime(item.deleted_at),
        "revision_count": item.revision_count,
        "discovered_by": item.discovered_by,
        "state": file_state(item),
    }


def event_dict(item: ActivityEvent) -> dict[str, Any]:
    path = item.path or item.new_path or item.old_path
    return {
        "id": item.id,
        "watch_id": item.watch_id,
        "file_id": item.file_id,
        "event_type": item.event_type.value,
        "event_label": EVENT_LABELS[item.event_type.value],
        "node_type": item.node_type,
        "node_label": "Папка" if item.node_type == "folder" else "Файл",
        "event_time": api_datetime(item.event_time),
        "path": path,
        "old_path": item.old_path,
        "new_path": item.new_path,
        "size": item.size,
        "etag": item.etag,
        "actor_user_id": item.actor_user_id,
        "file": {
            "filename": item.file.filename if item.file else PurePosixPath(path).name,
            "relative_path": item.file.relative_path if item.file else "",
        },
    }


def report_schedule_dict(item: ReportSchedule | None) -> dict[str, Any] | None:
    if item is None:
        return None
    return {
        "id": item.id,
        "watch_id": item.watch_id,
        "enabled": item.enabled,
        "cadence": item.cadence.value,
        "local_time": item.local_time,
        "weekday": item.weekday,
        "day_of_month": item.day_of_month,
        "destination_path": item.destination_path,
        "next_run_at": api_datetime(item.next_run_at),
        "last_started_at": api_datetime(item.last_started_at),
        "last_finished_at": api_datetime(item.last_finished_at),
        "last_status": item.last_status,
        "last_error": item.last_error,
        "last_file_path": item.last_file_path,
    }
