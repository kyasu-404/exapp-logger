from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote
from xml.etree import ElementTree

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..activity_keys import activity_event_key
from ..db import SessionFactory
from ..models import ActivityEvent, EventType, FileRecord, FolderRecord, Watch, WatchStatus

LOGGER = logging.getLogger(__name__)

EVENT_MAP = {
    "NodeCreatedEvent": EventType.CREATED,
    "NodeWrittenEvent": EventType.UPDATED,
    "NodeRenamedEvent": EventType.MOVED,
    "NodeDeletedEvent": EventType.DELETED,
    "NodeCopiedEvent": EventType.COPIED,
}
FULL_EVENT_CLASSES = {name: f"OCP\\Files\\Events\\Node\\{name}" for name in EVENT_MAP}
USER_FILE_RE = re.compile(r"^/?(?P<user>[^/]+)/files/(?P<path>.*)$")
UPLOAD_TIME_TAG = "{http://nextcloud.org/ns}upload_time"
_GROUP_CACHE: dict[str, tuple[float, set[str]]] = {}


def utcnow() -> datetime:
    return datetime.now(UTC)


def normalize_path(value: str) -> str:
    normalized = "/" + str(value or "").replace("\\", "/").strip("/")
    return normalized if normalized != "" else "/"


def relative_to_watch(path: str, root_path: str) -> str | None:
    path = normalize_path(path)
    root = normalize_path(root_path).rstrip("/")
    if path == root:
        return ""
    prefix = root + "/"
    return path[len(prefix) :] if path.startswith(prefix) else None


def relative_prefix_matches(path: str, prefix: str) -> bool:
    normalized_path = str(path or "").replace("\\", "/").strip("/").casefold()
    normalized_prefix = str(prefix or "").replace("\\", "/").strip("/").casefold()
    return (
        not normalized_prefix
        or normalized_path == normalized_prefix
        or normalized_path.startswith(normalized_prefix + "/")
    )


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo:
        return value
    return value.replace(tzinfo=UTC)


def _timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        return _aware(value) or utcnow()
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=UTC)
    if isinstance(value, str):
        raw = value.strip()
        if raw.isdigit():
            return datetime.fromtimestamp(int(raw), tz=UTC)
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return _aware(parsed) or utcnow()
        except ValueError:
            pass
    return utcnow()


def _optional_timestamp(value: Any) -> datetime | None:
    if value in (None, "", 0, "0"):
        return None
    return _timestamp(value)


@dataclass(slots=True)
class NodeSnapshot:
    owner_user_id: str
    path: str
    file_id: str | None = None
    etag: str = ""
    mime_type: str = "application/octet-stream"
    size: int = 0
    mtime: datetime | None = None
    upload_time: datetime | None = None
    is_dir: bool = False

    @property
    def filename(self) -> str:
        return PurePosixPath(self.path).name

    @property
    def parent_path(self) -> str:
        parent = str(PurePosixPath(self.path).parent)
        return "/" if parent == "." else normalize_path(parent)


@dataclass(slots=True)
class NormalizedEvent:
    subtype: str
    event_type: EventType
    target: NodeSnapshot
    source: NodeSnapshot | None
    occurred_at: datetime
    actor_user_id: str
    raw: dict[str, Any]


def _legacy_node(data: dict[str, Any]) -> NodeSnapshot:
    directory = str(data.get("directory") or "/")
    name = str(data.get("name") or "")
    file_type = str(data.get("fileType") or data.get("file_type") or "file").lower()
    return NodeSnapshot(
        owner_user_id=str(data.get("userId") or data.get("user_id") or ""),
        path=normalize_path(f"{directory.rstrip('/')}/{name}"),
        file_id=str(data.get("fileId") or data.get("file_id") or "") or None,
        etag=str(data.get("etag") or "").strip('"'),
        mime_type=str(data.get("mime") or "application/octet-stream"),
        size=int(data.get("size") or 0),
        mtime=_optional_timestamp(data.get("mtime")),
        upload_time=_optional_timestamp(data.get("upload_time") or data.get("uploadTime")),
        is_dir=file_type == "dir",
    )


def _webhook_path_node(data: dict[str, Any], fallback_user: str = "") -> NodeSnapshot:
    raw_path = str(data.get("path") or "")
    match = USER_FILE_RE.match(raw_path)
    if match:
        owner = match.group("user")
        path = normalize_path(match.group("path"))
    else:
        owner = fallback_user
        path = normalize_path(raw_path)
    return NodeSnapshot(owner_user_id=owner, path=path, file_id=str(data.get("id") or "") or None)


async def normalize_event(payload: dict[str, Any]) -> NormalizedEvent:
    if payload.get("event_type") == "node_event":
        subtype = str(payload.get("event_subtype") or "")
        if subtype not in EVENT_MAP:
            raise ValueError(f"Неподдерживаемое событие: {subtype}")
        event_data = payload.get("event_data") or {}
        target = _legacy_node(event_data.get("target") or {})
        source_data = event_data.get("source")
        source = _legacy_node(source_data) if source_data else None
        return NormalizedEvent(subtype, EVENT_MAP[subtype], target, source, utcnow(), target.owner_user_id, payload)

    envelope = payload.get("event") or {}
    event_class = str(envelope.get("class") or "")
    subtype = event_class.rsplit("\\", 1)[-1]
    if subtype not in EVENT_MAP:
        raise ValueError(f"Неподдерживаемое событие: {event_class or 'не указано'}")
    user = payload.get("user") or {}
    actor = str(user.get("uid") or "")
    if subtype in {"NodeRenamedEvent", "NodeCopiedEvent"}:
        target = _webhook_path_node(envelope.get("target") or {}, actor)
        source = _webhook_path_node(envelope.get("source") or {}, actor)
    else:
        target = _webhook_path_node(envelope.get("node") or {}, actor)
        source = None
    event = NormalizedEvent(
        subtype, EVENT_MAP[subtype], target, source, _timestamp(payload.get("time")), actor, payload
    )
    if event.event_type != EventType.DELETED:
        event.target = await enrich_snapshot(event.target)
    return event


async def _nextcloud_upload_time(nc, owner: str, path: str) -> datetime | None:
    """Read Nextcloud's nc:upload_time DAV property (not exposed by nc-py-api 0.30)."""
    try:
        from nc_py_api.files._files import PropFindType, build_listdir_req, element_tree_as_str

        root, dav_path = build_listdir_req(owner, path.lstrip("/"), ["nc:upload_time"], PropFindType.DEFAULT)
        response = await nc.files._session.adapter_dav.request(  # noqa: SLF001
            "PROPFIND", quote(dav_path), data=element_tree_as_str(root), headers={"Depth": "0"}
        )
        if response.status_code >= 400:
            return None
        document = ElementTree.fromstring(response.content)
        value = document.findtext(f".//{UPLOAD_TIME_TAG}")
        return _optional_timestamp(value)
    except Exception as exc:
        LOGGER.debug("Не удалось получить nc:upload_time для %s: %s", path, exc)
        return None


async def enrich_snapshot(snapshot: NodeSnapshot) -> NodeSnapshot:
    if not snapshot.owner_user_id:
        return snapshot
    try:
        from nc_py_api import AsyncNextcloudApp

        nc = AsyncNextcloudApp(user=snapshot.owner_user_id)
        node = await nc.files.by_id(snapshot.file_id) if snapshot.file_id else None
        if node is None:
            node = await nc.files.by_path(snapshot.path.lstrip("/"))
        if node is None:
            return snapshot
        path = normalize_path(node.user_path)
        return NodeSnapshot(
            owner_user_id=snapshot.owner_user_id,
            path=path,
            file_id=str(node.info.fileid or snapshot.file_id or "") or None,
            etag=node.etag_unquoted,
            mime_type=node.info.mimetype or "application/octet-stream",
            size=int(node.info.content_length or node.info.size or 0),
            mtime=node.info.last_modified,
            upload_time=await _nextcloud_upload_time(nc, snapshot.owner_user_id, path),
            is_dir=node.is_dir,
        )
    except Exception as exc:
        LOGGER.warning("Не удалось дочитать метаданные узла %s: %s", snapshot.path, exc)
        return snapshot


def _watch_accepts_event(watch: Watch, event_time: datetime) -> bool:
    moment = _aware(event_time)
    started = _aware(watch.started_at)
    ended = _aware(watch.ended_at)
    return not ((started and moment < started) or (ended and moment > ended))


async def _active_watches(session: AsyncSession) -> list[Watch]:
    return list((await session.scalars(select(Watch).where(Watch.status == WatchStatus.ACTIVE))).all())


async def _event_user_groups(user_id: str) -> set[str]:
    if not user_id:
        return set()
    cached = _GROUP_CACHE.get(user_id)
    now = time.monotonic()
    if cached and now - cached[0] < 300:
        return cached[1]
    try:
        from nc_py_api import AsyncNextcloudApp

        info = await AsyncNextcloudApp(user=user_id).users.get_user()
        groups = set(info.groups)
    except Exception as exc:
        LOGGER.warning("Не удалось определить группы автора события %s: %s", user_id, exc)
        groups = set()
    _GROUP_CACHE[user_id] = (now, groups)
    return groups


def _scope_accepts_owner(watch: Watch, node_owner: str, actor_groups: set[str]) -> bool:
    if watch.scope_type != "team":
        return watch.owner_user_id == node_owner
    return node_owner == watch.owner_user_id or bool(set(watch.group_ids or []).intersection(actor_groups))


async def _find_file(session: AsyncSession, watch_id: int, snapshot: NodeSnapshot) -> FileRecord | None:
    predicates = [FileRecord.full_path == snapshot.path]
    if snapshot.file_id:
        predicates.insert(0, FileRecord.nextcloud_file_id == snapshot.file_id)
    return await session.scalar(
        select(FileRecord).where(FileRecord.watch_id == watch_id, or_(*predicates)).order_by(FileRecord.id.desc())
    )


async def _find_folder(session: AsyncSession, watch_id: int, snapshot: NodeSnapshot) -> FolderRecord | None:
    predicates = [FolderRecord.full_path == snapshot.path]
    if snapshot.file_id:
        predicates.insert(0, FolderRecord.nextcloud_file_id == snapshot.file_id)
    return await session.scalar(
        select(FolderRecord).where(FolderRecord.watch_id == watch_id, or_(*predicates)).order_by(FolderRecord.id.desc())
    )


async def _record_activity(
    session: AsyncSession,
    watch: Watch,
    file: FileRecord | None,
    event: NormalizedEvent,
    event_type: EventType,
    snapshot: NodeSnapshot,
    old_path: str = "",
    node_type: str = "file",
) -> bool:
    node_identity = snapshot.file_id or (file.nextcloud_file_id if file else None)
    if event_type == EventType.UPDATED:
        node_identity = f"{node_identity or (file.id if file else snapshot.path)}:{snapshot.etag or snapshot.mtime}"
    key = activity_event_key(
        watch.id,
        event_type,
        node_identity,
        snapshot.path,
        old_path,
        event.occurred_at,
    )
    if await session.scalar(select(ActivityEvent.id).where(ActivityEvent.event_key == key)):
        return False
    session.add(
        ActivityEvent(
            watch_id=watch.id,
            file_id=file.id if file else None,
            event_key=key,
            event_type=event_type,
            node_type=node_type,
            event_time=event.occurred_at,
            path=snapshot.path,
            old_path=old_path,
            new_path=snapshot.path if event_type != EventType.DELETED else "",
            etag=snapshot.etag,
            size=snapshot.size,
            actor_user_id=event.actor_user_id,
            raw_event=event.raw,
        )
    )
    return True


def _file_parts(watch: Watch, snapshot: NodeSnapshot) -> tuple[str, str, str]:
    relative = relative_to_watch(snapshot.path, watch.root_path)
    if relative is None:
        raise ValueError("Путь находится вне наблюдения")
    parent = str(PurePosixPath(relative).parent)
    if parent == ".":
        parent = ""
    extension = PurePosixPath(snapshot.filename).suffix.lower().lstrip(".")
    return relative, parent, extension


async def _upsert_file(
    session: AsyncSession,
    watch: Watch,
    event: NormalizedEvent,
    event_type: EventType,
    snapshot: NodeSnapshot,
    old_path: str = "",
) -> bool:
    if snapshot.is_dir:
        return await _upsert_folder(session, watch, event, event_type, snapshot, old_path)
    relative, parent, extension = _file_parts(watch, snapshot)
    file = await _find_file(session, watch.id, snapshot)
    is_new = file is None
    if file is None:
        file = FileRecord(
            watch_id=watch.id,
            nextcloud_file_id=snapshot.file_id,
            filename=snapshot.filename,
            full_path=snapshot.path,
            relative_path=relative,
            parent_path=parent,
            mime_type=snapshot.mime_type,
            extension=extension,
            size=snapshot.size,
            etag=snapshot.etag,
            first_seen_at=event.occurred_at,
            last_seen_at=event.occurred_at,
            nextcloud_upload_time=snapshot.upload_time,
            nextcloud_mtime=snapshot.mtime,
            discovered_by="event",
        )
        session.add(file)
        await session.flush()
    old_etag = file.etag
    old_size = file.size
    old_mtime = file.nextcloud_mtime
    if event_type == EventType.UPDATED:
        changed = is_new or any(
            (
                bool(snapshot.etag and snapshot.etag != old_etag),
                snapshot.size != old_size,
                bool(snapshot.mtime and snapshot.mtime != old_mtime),
            )
        )
        if not changed:
            return False
        await _record_activity(session, watch, file, event, event_type, snapshot, old_path)
    elif not await _record_activity(session, watch, file, event, event_type, snapshot, old_path):
        return False

    file.nextcloud_file_id = snapshot.file_id or file.nextcloud_file_id
    file.filename = snapshot.filename
    file.full_path = snapshot.path
    file.relative_path = relative
    file.parent_path = parent
    file.mime_type = snapshot.mime_type or file.mime_type
    file.extension = extension
    file.size = snapshot.size
    file.etag = snapshot.etag or file.etag
    file.nextcloud_upload_time = snapshot.upload_time or file.nextcloud_upload_time
    file.nextcloud_mtime = snapshot.mtime or file.nextcloud_mtime
    file.last_seen_at = event.occurred_at
    file.exists = True
    file.deleted_at = None
    if event_type == EventType.UPDATED and not is_new:
        file.revision_count += 1
    return True


async def _upsert_folder(
    session: AsyncSession,
    watch: Watch,
    event: NormalizedEvent,
    event_type: EventType,
    snapshot: NodeSnapshot,
    old_path: str = "",
) -> bool:
    relative = relative_to_watch(snapshot.path, watch.root_path)
    if relative is None or relative == "":
        return False
    folder = await _find_folder(session, watch.id, snapshot)
    if not await _record_activity(session, watch, None, event, event_type, snapshot, old_path, node_type="folder"):
        return False
    if folder is None:
        folder = FolderRecord(
            watch_id=watch.id,
            nextcloud_file_id=snapshot.file_id,
            name=snapshot.filename,
            full_path=snapshot.path,
            relative_path=relative,
            first_seen_at=event.occurred_at,
            last_seen_at=event.occurred_at,
        )
        session.add(folder)
    else:
        folder.nextcloud_file_id = snapshot.file_id or folder.nextcloud_file_id
        folder.name = snapshot.filename
        folder.full_path = snapshot.path
        folder.relative_path = relative
        folder.last_seen_at = event.occurred_at
        folder.exists = True
        folder.deleted_at = None
    return True


async def _mark_missing(
    session: AsyncSession,
    watch: Watch,
    event: NormalizedEvent,
    event_type: EventType,
    snapshot: NodeSnapshot,
    new_path: str = "",
) -> bool:
    if snapshot.is_dir:
        return await _mark_folder_missing(session, watch, event, event_type, snapshot, new_path)
    file = await _find_file(session, watch.id, snapshot)
    if file is None:
        return await _mark_folder_missing(session, watch, event, event_type, snapshot, new_path)
    target = NodeSnapshot(
        owner_user_id=snapshot.owner_user_id,
        path=new_path or snapshot.path,
        file_id=snapshot.file_id,
        etag=file.etag,
        mime_type=file.mime_type,
        size=file.size,
        mtime=file.nextcloud_mtime,
        upload_time=file.nextcloud_upload_time,
    )
    if not await _record_activity(session, watch, file, event, event_type, target, snapshot.path):
        return False
    file.exists = False
    file.deleted_at = event.occurred_at
    file.last_seen_at = event.occurred_at
    return True


async def _mark_folder_missing(
    session: AsyncSession,
    watch: Watch,
    event: NormalizedEvent,
    event_type: EventType,
    snapshot: NodeSnapshot,
    new_path: str = "",
) -> bool:
    folder = await _find_folder(session, watch.id, snapshot)
    if folder is None:
        return False
    target = NodeSnapshot(
        owner_user_id=snapshot.owner_user_id,
        path=new_path or snapshot.path,
        file_id=snapshot.file_id or folder.nextcloud_file_id,
        is_dir=True,
    )
    if not await _record_activity(session, watch, None, event, event_type, target, snapshot.path, node_type="folder"):
        return False
    folder.exists = False
    folder.deleted_at = event.occurred_at
    folder.last_seen_at = event.occurred_at
    return True


def _rename_type(source: NodeSnapshot, target: NodeSnapshot) -> EventType:
    return EventType.MOVED if source.parent_path != target.parent_path else EventType.RENAMED


async def process_event_payload(payload: dict[str, Any]) -> dict[str, Any]:
    event = await normalize_event(payload)
    owners = {node.owner_user_id for node in (event.target, event.source) if node and node.owner_user_id}
    if not owners:
        return {"matched": 0, "changed": 0}

    matched = changed = 0
    async with SessionFactory() as session:
        watches = await _active_watches(session)
        actor_groups = (
            await _event_user_groups(event.actor_user_id or event.target.owner_user_id)
            if any(watch.scope_type == "team" for watch in watches)
            else set()
        )
        for watch in watches:
            if not _watch_accepts_event(watch, event.occurred_at):
                continue
            target_inside = (
                _scope_accepts_owner(watch, event.target.owner_user_id, actor_groups)
                and relative_to_watch(event.target.path, watch.root_path) is not None
            )
            source_inside = bool(
                event.source
                and _scope_accepts_owner(watch, event.source.owner_user_id, actor_groups)
                and relative_to_watch(event.source.path, watch.root_path) is not None
            )
            if not target_inside and not source_inside:
                continue
            matched += 1
            watch.last_webhook_at = utcnow()

            did_change = False
            if event.subtype == "NodeDeletedEvent" and target_inside:
                did_change = await _mark_missing(session, watch, event, EventType.DELETED, event.target)
            elif event.subtype == "NodeRenamedEvent":
                source = event.source or event.target
                move_type = _rename_type(source, event.target)
                if source_inside and target_inside:
                    did_change = await _upsert_file(
                        session, watch, event, move_type, event.target, old_path=source.path
                    )
                elif source_inside:
                    did_change = await _mark_missing(
                        session, watch, event, EventType.MOVED, source, new_path=event.target.path
                    )
                elif target_inside:
                    did_change = await _upsert_file(
                        session, watch, event, EventType.MOVED, event.target, old_path=source.path
                    )
            elif event.subtype == "NodeCopiedEvent" and target_inside:
                did_change = await _upsert_file(
                    session,
                    watch,
                    event,
                    EventType.COPIED,
                    event.target,
                    old_path=event.source.path if event.source else "",
                )
            elif target_inside:
                did_change = await _upsert_file(session, watch, event, event.event_type, event.target)

            changed += int(did_change)
        await session.commit()
    return {"matched": matched, "changed": changed}
