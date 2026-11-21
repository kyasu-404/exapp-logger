from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any


def _event_type_value(value: Any) -> str:
    return str(getattr(value, "value", value)).lower()


def _path(value: str) -> str:
    normalized = "/" + str(value or "").replace("\\", "/").strip("/")
    return normalized if normalized != "" else "/"


def _time_value(value: datetime | str | None) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return str(value or "")


def revision_identity(node: str | int, etag: str, mtime: datetime | None, size: int) -> str:
    return f"{node}:{etag or f'{_time_value(mtime)}:{size}'}"


def activity_event_key(
    watch_id: int,
    event_type: Any,
    node_identity: str | int | None,
    path: str,
    old_path: str = "",
    event_time: datetime | str | None = None,
    *,
    previous_activity_id: int | None = None,
) -> str:
    """Return one semantic key for webhook and reconciliation activity."""
    identity = {
        "watch": watch_id,
        "type": _event_type_value(event_type),
        "node": str(node_identity or ""),
        "path": _path(path),
        "old_path": _path(old_path) if old_path else "",
    }
    # Deleted folder events may not carry a node id. Their event timestamp keeps
    # a later delete/recreate/delete cycle distinct while still deduplicating the
    # same queued delivery.
    if not identity["node"]:
        identity["event_time"] = _time_value(event_time)
    if previous_activity_id is not None:
        identity["previous_activity_id"] = previous_activity_id
    payload = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()
