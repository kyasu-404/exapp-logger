from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from pathlib import PurePosixPath
from urllib.parse import quote
from xml.etree import ElementTree

from sqlalchemy import select

from ..activity_keys import activity_event_key, revision_identity
from ..db import SessionFactory
from ..models import ActivityEvent, EventType, FileRecord, FolderRecord, ReconciliationRun, Watch, WatchStatus
from .events import (
    NodeSnapshot,
    _optional_timestamp,
    last_activity_id,
    normalize_path,
    relative_to_watch,
    watch_state_locks,
)

LOGGER = logging.getLogger(__name__)
UPLOAD_TIME_TAG = "{http://nextcloud.org/ns}upload_time"
FILE_ID_TAG = "{http://owncloud.org/ns}fileid"
_LOCKS: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


class SnapshotSuperseded(Exception):
    """The watch changed while its DAV tree was being collected."""


def utcnow() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo:
        return value
    return value.replace(tzinfo=UTC)


def _same_timestamp(left: datetime | None, right: datetime | None) -> bool:
    return _aware(left) == _aware(right)


def refresh_file_metadata(
    file: FileRecord,
    snapshot: NodeSnapshot,
    relative: str,
    parent: str,
    extension: str,
) -> tuple[bool, bool, bool, bool]:
    """Apply the authoritative DAV snapshot and describe what changed."""
    path_changed = file.full_path != snapshot.path
    content_changed = file.etag != snapshot.etag or file.size != snapshot.size
    restored = not file.exists
    upload_time = snapshot.upload_time or file.nextcloud_upload_time
    mtime = snapshot.mtime or file.nextcloud_mtime
    metadata_changed = any(
        (
            file.nextcloud_file_id != (snapshot.file_id or file.nextcloud_file_id),
            file.filename != snapshot.filename,
            file.relative_path != relative,
            file.parent_path != parent,
            file.mime_type != snapshot.mime_type,
            file.extension != extension,
            not _same_timestamp(file.nextcloud_upload_time, upload_time),
            not _same_timestamp(file.nextcloud_mtime, mtime),
        )
    )

    file.nextcloud_file_id = snapshot.file_id or file.nextcloud_file_id
    file.filename = snapshot.filename
    file.full_path = snapshot.path
    file.relative_path = relative
    file.parent_path = parent
    file.mime_type = snapshot.mime_type
    file.extension = extension
    file.size = snapshot.size
    file.etag = snapshot.etag
    file.nextcloud_upload_time = upload_time
    file.nextcloud_mtime = mtime
    file.exists = True
    file.deleted_at = None
    return path_changed, content_changed, restored, metadata_changed


async def _tree_snapshots(nc, owner: str, root_path: str) -> list[NodeSnapshot]:
    """Read the complete DAV tree and upload times in one request."""
    try:
        from nc_py_api.files._files import (
            PropFindType,
            build_listdir_req,
            build_listdir_response,
            element_tree_as_str,
            get_propfind_properties,
        )

        properties = get_propfind_properties(await nc.files._session.capabilities)  # noqa: SLF001
        properties.append("nc:upload_time")
        relative_root = root_path.lstrip("/")
        root, dav_path = build_listdir_req(owner, relative_root, properties, PropFindType.DEFAULT)
        response = await nc.files._session.adapter_dav.request(  # noqa: SLF001
            "PROPFIND", quote(dav_path), data=element_tree_as_str(root), headers={"Depth": "infinity"}
        )
        upload_times: dict[str, datetime] = {}
        document = ElementTree.fromstring(response.content)
        for response_node in document.findall(".//{DAV:}response"):
            file_id = response_node.findtext(f".//{FILE_ID_TAG}")
            upload_time = _optional_timestamp(response_node.findtext(f".//{UPLOAD_TIME_TAG}"))
            if file_id and upload_time:
                upload_times[str(file_id)] = upload_time
        nodes = build_listdir_response(
            nc.files._session.cfg.dav_url_suffix,  # noqa: SLF001
            response,
            owner,
            relative_root,
            properties,
            True,
            PropFindType.DEFAULT,
        )
        return [snapshot_from_fs_node(owner, node, upload_times) for node in nodes]
    except Exception as exc:
        LOGGER.warning("Не удалось получить дерево DAV для %s: %s", root_path, exc)
        raise


def snapshot_from_fs_node(owner: str, node, upload_times: dict[str, datetime] | None = None) -> NodeSnapshot:
    file_id = str(node.info.fileid or "") or None
    return NodeSnapshot(
        owner_user_id=owner,
        path=normalize_path(node.user_path),
        file_id=file_id,
        etag=node.etag_unquoted,
        mime_type=node.info.mimetype or "application/octet-stream",
        size=int(node.info.content_length or node.info.size or 0),
        mtime=node.info.last_modified,
        upload_time=(upload_times or {}).get(file_id or ""),
        is_dir=node.is_dir,
    )


def _parts(watch: Watch, snapshot: NodeSnapshot) -> tuple[str, str, str]:
    relative = relative_to_watch(snapshot.path, watch.root_path)
    if relative is None:
        raise ValueError("Файл находится вне наблюдаемой папки")
    parent = str(PurePosixPath(relative).parent)
    if parent == ".":
        parent = ""
    extension = PurePosixPath(snapshot.filename).suffix.lower().lstrip(".")
    return relative, parent, extension


async def _event(
    session,
    file: FileRecord | None,
    event_type: EventType,
    path: str,
    actor: str,
    *,
    old_path: str = "",
    event_time: datetime | None = None,
    watch_id: int | None = None,
    node_identity: str = "",
    node_type: str = "file",
    size: int = 0,
    etag: str = "",
    existing_node_transition: bool = False,
) -> bool:
    occurred_at = event_time or utcnow()
    resolved_watch_id = file.watch_id if file else watch_id
    if resolved_watch_id is None:
        raise ValueError("Не указан идентификатор наблюдения")
    identity = node_identity or (file.nextcloud_file_id if file else "") or (str(file.id) if file else path)
    resolved_etag = etag or (file.etag if file else "")
    if event_type == EventType.UPDATED:
        identity = revision_identity(
            identity, resolved_etag, file.nextcloud_mtime if file else None, file.size if file else size
        )
    key = activity_event_key(
        resolved_watch_id,
        event_type,
        identity,
        path,
        old_path,
        occurred_at,
        previous_activity_id=await last_activity_id(session, resolved_watch_id) if existing_node_transition else None,
    )
    if await session.scalar(select(ActivityEvent.id).where(ActivityEvent.event_key == key)):
        return False
    session.add(
        ActivityEvent(
            watch_id=resolved_watch_id,
            file_id=file.id if file else None,
            event_key=key,
            event_type=event_type,
            node_type=node_type,
            event_time=occurred_at,
            path=path,
            old_path=old_path,
            new_path=path if event_type != EventType.DELETED else "",
            etag=resolved_etag,
            size=file.size if file else size,
            actor_user_id=actor,
            raw_event={"source": "reconciliation"},
        )
    )
    return True


def watch_is_current(watch: Watch, moment: datetime | None = None) -> bool:
    now = _aware(moment or utcnow())
    started = _aware(watch.started_at)
    ended = _aware(watch.ended_at)
    return bool(
        watch.status == WatchStatus.ACTIVE and (started is None or started <= now) and (ended is None or now <= ended)
    )


@asynccontextmanager
async def _stable_dav_tree(watch: Watch):
    from nc_py_api import AsyncNextcloudApp

    for _ in range(2):
        nc = AsyncNextcloudApp(user=watch.owner_user_id)
        # Webhook timestamps have second precision. A later delivery can still
        # describe a change made during the DAV request.
        observed_at = utcnow().replace(microsecond=0)
        snapshots = await _tree_snapshots(nc, watch.owner_user_id, watch.root_path)
        async with watch_state_locks[watch.id], SessionFactory() as session:
            current = await session.get(Watch, watch.id)
            if current is None or not watch_is_current(current):
                raise SnapshotSuperseded("Наблюдение изменилось во время сверки")
            if (
                current.last_webhook_at == watch.last_webhook_at
                and current.root_path == watch.root_path
                and current.owner_user_id == watch.owner_user_id
            ):
                yield session, current, snapshots, observed_at
                return
            watch = current
    raise SnapshotSuperseded("Во время сверки поступили новые события. Сверка будет повторена по расписанию.")


def _index_nodes(records):
    ordered = sorted(records, key=lambda record: (record.exists, record.id))
    by_id = {record.nextcloud_file_id: record for record in ordered if record.nextcloud_file_id}
    by_path = {record.full_path: record for record in ordered}
    unidentified = {record.full_path: record for record in ordered if not record.nextcloud_file_id}
    return by_id, by_path, unidentified


async def reconcile_watch(watch_id: int, triggered_by: str = "system") -> dict[str, int | str]:
    async with _LOCKS[watch_id]:
        async with SessionFactory() as session:
            watch = await session.get(Watch, watch_id)
            if watch is None:
                raise ValueError("Наблюдение не найдено")
            if not watch_is_current(watch):
                return {
                    "status": "skipped",
                    "reason": "Наблюдение не активно или текущая дата вне его периода",
                    "checked": 0,
                    "created": 0,
                    "updated": 0,
                    "missing": 0,
                }
            run = ReconciliationRun(watch_id=watch.id, triggered_by=triggered_by)
            session.add(run)
            await session.commit()
            run_id = run.id

        try:
            async with _stable_dav_tree(watch) as (session, watch, snapshots, now):
                file_snapshots = [snapshot for snapshot in snapshots if not snapshot.is_dir]
                folder_snapshots = [snapshot for snapshot in snapshots if snapshot.is_dir]
                checked = len(snapshots)
                created = updated = missing = 0
                existing = list(
                    (await session.scalars(select(FileRecord).where(FileRecord.watch_id == watch_id))).all()
                )
                existing_folders = list(
                    (await session.scalars(select(FolderRecord).where(FolderRecord.watch_id == watch_id))).all()
                )
                by_id, by_path, unidentified = _index_nodes(existing)
                seen_ids: set[int] = set()

                folders_by_id, folders_by_path, unidentified_folders = _index_nodes(existing_folders)
                seen_folder_ids: set[int] = set()

                for snapshot in folder_snapshots:
                    relative = relative_to_watch(snapshot.path, watch.root_path)
                    if relative in (None, ""):
                        continue
                    folder = (
                        folders_by_id.get(snapshot.file_id) or unidentified_folders.get(snapshot.path)
                        if snapshot.file_id else folders_by_path.get(snapshot.path)
                    )
                    if folder is None:
                        folder = FolderRecord(
                            watch_id=watch_id,
                            nextcloud_file_id=snapshot.file_id,
                            name=snapshot.filename,
                            full_path=snapshot.path,
                            relative_path=relative,
                            first_seen_at=now,
                            last_seen_at=now,
                        )
                        session.add(folder)
                        await session.flush()
                        await _event(
                            session,
                            None,
                            EventType.CREATED,
                            snapshot.path,
                            triggered_by,
                            watch_id=watch_id,
                            node_identity=snapshot.file_id or str(folder.id),
                            node_type="folder",
                        )
                        created += 1
                    else:
                        old_path = folder.full_path
                        path_changed = old_path != snapshot.path
                        restored = not folder.exists
                        folder.nextcloud_file_id = snapshot.file_id or folder.nextcloud_file_id
                        folder.name = snapshot.filename
                        folder.full_path = snapshot.path
                        folder.relative_path = relative
                        folder.exists = True
                        folder.deleted_at = None
                        if path_changed or restored:
                            folder.last_seen_at = now
                            await _event(
                                session,
                                None,
                                EventType.MOVED if path_changed else EventType.CREATED,
                                snapshot.path,
                                triggered_by,
                                old_path=old_path if path_changed else "",
                                watch_id=watch_id,
                                node_identity=snapshot.file_id or str(folder.id),
                                node_type="folder",
                                existing_node_transition=True,
                            )
                            updated += 1
                    seen_folder_ids.add(folder.id)

                for snapshot in file_snapshots:
                    file = (
                        by_id.get(snapshot.file_id) or unidentified.get(snapshot.path)
                        if snapshot.file_id else by_path.get(snapshot.path)
                    )
                    relative, parent, extension = _parts(watch, snapshot)
                    if file is None:
                        file = FileRecord(
                            watch_id=watch_id,
                            nextcloud_file_id=snapshot.file_id,
                            filename=snapshot.filename,
                            full_path=snapshot.path,
                            relative_path=relative,
                            parent_path=parent,
                            mime_type=snapshot.mime_type,
                            extension=extension,
                            size=snapshot.size,
                            etag=snapshot.etag,
                            first_seen_at=now,
                            last_seen_at=now,
                            nextcloud_upload_time=snapshot.upload_time,
                            nextcloud_mtime=snapshot.mtime,
                            discovered_by="reconciliation",
                        )
                        session.add(file)
                        await session.flush()
                        await _event(session, file, EventType.CREATED, snapshot.path, triggered_by, event_time=now)
                        created += 1
                    else:
                        old_path = file.full_path
                        path_changed, content_changed, restored, metadata_changed = refresh_file_metadata(
                            file, snapshot, relative, parent, extension
                        )
                        if path_changed or content_changed or restored or metadata_changed:
                            if path_changed or content_changed or restored:
                                file.last_seen_at = now
                            if path_changed or content_changed or restored:
                                event_type = (
                                    EventType.MOVED
                                    if path_changed
                                    else EventType.CREATED
                                    if restored
                                    else EventType.UPDATED
                                )
                                recorded = await _event(
                                    session,
                                    file,
                                    event_type,
                                    snapshot.path,
                                    triggered_by,
                                    old_path=old_path if path_changed else "",
                                    existing_node_transition=path_changed or restored,
                                )
                                if recorded and content_changed:
                                    file.revision_count += 1
                            updated += 1
                    seen_ids.add(file.id)

                for file in existing:
                    if file.id in seen_ids or not file.exists:
                        continue
                    file.exists = False
                    file.deleted_at = now
                    file.last_seen_at = now
                    await _event(
                        session,
                        file,
                        EventType.DELETED,
                        file.full_path,
                        triggered_by,
                        old_path=file.full_path,
                        existing_node_transition=True,
                    )
                    missing += 1

                for folder in existing_folders:
                    if folder.id in seen_folder_ids or not folder.exists:
                        continue
                    folder.exists = False
                    folder.deleted_at = now
                    folder.last_seen_at = now
                    await _event(
                        session,
                        None,
                        EventType.DELETED,
                        folder.full_path,
                        triggered_by,
                        old_path=folder.full_path,
                        watch_id=watch_id,
                        node_identity=folder.nextcloud_file_id or str(folder.id),
                        node_type="folder",
                        existing_node_transition=True,
                    )
                    missing += 1

                completed_run = await session.get(ReconciliationRun, run_id)
                if completed_run:
                    completed_run.status = "completed"
                    completed_run.finished_at = utcnow()
                    completed_run.checked_count = checked
                    completed_run.created_count = created
                    completed_run.updated_count = updated
                    completed_run.missing_count = missing
                await session.commit()

            LOGGER.info(
                "Сверка наблюдения %s: проверено=%d новых=%d обновлено=%d отсутствует=%d",
                watch_id,
                checked,
                created,
                updated,
                missing,
            )
            return {
                "status": "completed",
                "checked": checked,
                "created": created,
                "updated": updated,
                "missing": missing,
            }
        except SnapshotSuperseded as exc:
            async with SessionFactory() as session:
                skipped_run = await session.get(ReconciliationRun, run_id)
                if skipped_run:
                    skipped_run.status = "superseded"
                    skipped_run.finished_at = utcnow()
                    await session.commit()
            return {"status": "skipped", "reason": str(exc), "checked": 0, "created": 0, "updated": 0, "missing": 0}
        except Exception as exc:
            LOGGER.exception("Ошибка сверки наблюдения %s", watch_id)
            async with SessionFactory() as session:
                failed_run = await session.get(ReconciliationRun, run_id)
                if failed_run:
                    failed_run.status = "failed"
                    failed_run.finished_at = utcnow()
                    failed_run.error = str(exc)[:4000]
                    await session.commit()
            raise


async def reconcile_all_active() -> None:
    async with SessionFactory() as session:
        now = utcnow()
        watches = list((await session.scalars(select(Watch).where(Watch.status == WatchStatus.ACTIVE))).all())
        ids = [watch.id for watch in watches if watch_is_current(watch, now)]

    semaphore = asyncio.Semaphore(3)

    async def run_one(watch_id: int) -> None:
        async with semaphore:
            with suppress(Exception):
                await reconcile_watch(watch_id)

    await asyncio.gather(*(run_one(watch_id) for watch_id in ids))
