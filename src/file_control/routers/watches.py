from __future__ import annotations

import math
from datetime import UTC, datetime
from pathlib import PurePosixPath

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..config import get_settings
from ..db import get_session
from ..models import ActivityEvent, EventType, FileRecord, FolderRecord, ReconciliationRun, Watch, WatchStatus
from ..schemas import WatchCreate, WatchUpdate
from ..security import ensure_watch_access, filter_accessible_watches, require_admin, require_observer
from ..serialization import event_dict, file_dict, watch_dict
from ..services.events import relative_to_watch
from ..services.folder_scope import resolve_watch_scope
from ..services.reconciliation import reconcile_watch
from ..time_utils import api_datetime, start_of_local_day_utc

router = APIRouter(prefix="/api/watches", tags=["watches"])


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo:
        return value
    return value.replace(tzinfo=UTC)


async def _watch(session: AsyncSession, watch_id: int, user_id: str | None = None) -> Watch:
    watch = await session.get(Watch, watch_id)
    if watch is None:
        raise HTTPException(status_code=404, detail="Наблюдение не найдено")
    if user_id is not None:
        await ensure_watch_access(user_id, watch)
    return watch


async def _watch_counts(session: AsyncSession, watch_id: int) -> dict[str, int]:
    files = (
        await session.scalar(
            select(func.count(FileRecord.id)).where(FileRecord.watch_id == watch_id, FileRecord.exists.is_(True))
        )
        or 0
    )
    deleted = (
        await session.scalar(
            select(func.count(FileRecord.id)).where(FileRecord.watch_id == watch_id, FileRecord.exists.is_(False))
        )
        or 0
    )
    return {"files": files, "deleted": deleted}


async def _resolve_scope(
    root_path: str,
    admin_user: str,
    requested_scope: str,
    requested_groups: list[str] | None,
) -> tuple[str, list[str]]:
    try:
        return await resolve_watch_scope(
            root_path,
            admin_user,
            requested_scope,
            requested_groups,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Не удалось проверить тип папки через Nextcloud: {exc}",
        ) from exc


@router.get("")
async def list_watches(
    include_archived: bool = False,
    current_user: str = Depends(require_observer),
    session: AsyncSession = Depends(get_session),
):
    query = select(Watch).order_by(Watch.status, Watch.created_at.desc())
    if not include_archived:
        query = query.where(Watch.status != WatchStatus.ARCHIVED)
    watches = await filter_accessible_watches(
        current_user,
        list((await session.scalars(query)).all()),
    )
    result = []
    for item in watches:
        value = watch_dict(item)
        value["counts"] = await _watch_counts(session, item.id)
        result.append(value)
    return result


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_watch(
    payload: WatchCreate,
    admin_user: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    owner = (payload.owner_user_id or admin_user).strip()
    scope_type, group_ids = await _resolve_scope(
        payload.root_path,
        admin_user,
        payload.scope_type,
        payload.group_ids,
    )
    watch = Watch(
        name=payload.name,
        root_path=payload.root_path,
        owner_user_id=owner,
        scope_type=scope_type,
        group_ids=group_ids,
        started_at=payload.started_at,
        ended_at=payload.ended_at,
        status=payload.status,
        created_by=admin_user,
    )
    session.add(watch)
    await session.commit()
    await session.refresh(watch)
    return watch_dict(watch)


@router.get("/{watch_id}")
async def get_watch(
    watch_id: int,
    current_user: str = Depends(require_observer),
    session: AsyncSession = Depends(get_session),
):
    result = watch_dict(await _watch(session, watch_id, current_user))
    result["counts"] = await _watch_counts(session, watch_id)
    return result


@router.patch("/{watch_id}")
async def update_watch(
    watch_id: int,
    payload: WatchUpdate,
    _admin: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    watch = await _watch(session, watch_id)
    values = payload.model_dump(exclude_unset=True)
    if {"root_path", "owner_user_id"}.intersection(values):
        has_files = await session.scalar(select(func.count(FileRecord.id)).where(FileRecord.watch_id == watch_id))
        changed_root = values.get("root_path", watch.root_path) != watch.root_path
        changed_owner = values.get("owner_user_id", watch.owner_user_id) != watch.owner_user_id
        if has_files and (changed_root or changed_owner):
            raise HTTPException(
                status_code=409,
                detail="Папку нельзя изменить после появления файлов; создайте новое наблюдение",
            )
    root_path = values.get("root_path", watch.root_path)
    requested_scope = values.pop("scope_type", watch.scope_type)
    requested_groups = values.pop("group_ids", watch.group_ids)
    scope_type, group_ids = await _resolve_scope(
        root_path,
        _admin,
        requested_scope,
        requested_groups,
    )
    values["scope_type"] = scope_type
    values["group_ids"] = group_ids
    for field, value in values.items():
        setattr(watch, field, value)
    watch.group_ids = sorted({item.strip() for item in (watch.group_ids or []) if item.strip()})
    if _aware(watch.started_at) and _aware(watch.ended_at) and _aware(watch.ended_at) < _aware(watch.started_at):
        raise HTTPException(status_code=422, detail="Дата окончания не может быть раньше даты начала")
    await session.commit()
    await session.refresh(watch)
    return watch_dict(watch)


@router.delete("/{watch_id}", status_code=status.HTTP_204_NO_CONTENT)
async def archive_watch(
    watch_id: int,
    _admin: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    watch = await _watch(session, watch_id)
    watch.status = WatchStatus.ARCHIVED
    await session.commit()


async def build_folder_stats(session: AsyncSession, watch: Watch) -> list[dict]:
    folder_records = await session.execute(
        select(FolderRecord.relative_path, FolderRecord.first_seen_at, FolderRecord.last_seen_at)
        .where(FolderRecord.watch_id == watch.id, FolderRecord.exists.is_(True))
    )
    files = await session.execute(
        select(
            FileRecord.parent_path,
            func.count().filter(FileRecord.exists.is_(True)).label("live_files"),
            func.coalesce(func.sum(FileRecord.size).filter(FileRecord.exists.is_(True)), 0).label("live_size"),
            func.min(FileRecord.first_seen_at).label("first_seen_at"),
            func.max(FileRecord.last_seen_at).label("last_seen_at"),
        ).where(FileRecord.watch_id == watch.id).group_by(FileRecord.parent_path)
    )
    event_path = func.coalesce(
        func.nullif(ActivityEvent.path, ""), func.nullif(ActivityEvent.new_path, ""), ActivityEvent.old_path
    )
    event_counts = await session.execute(
        select(event_path, ActivityEvent.event_type, func.count())
        .where(ActivityEvent.watch_id == watch.id, ActivityEvent.event_type.in_((EventType.UPDATED, EventType.DELETED)))
        .group_by(event_path, ActivityEvent.event_type)
    )
    folders: dict[str, dict] = {}

    def ensure(path: str) -> dict:
        return folders.setdefault(
            path,
            {
                "path": path,
                "name": PurePosixPath(path).name,
                "depth": len(PurePosixPath(path).parts),
                "files": 0,
                "size": 0,
                "first_seen_at": None,
                "last_seen_at": None,
                "updates": 0,
                "deleted": 0,
            },
        )

    for folder in folder_records:
        parts = [part for part in PurePosixPath(folder.relative_path).parts if part not in {"/", "", "."}]
        for depth in range(1, len(parts) + 1):
            item = ensure("/".join(parts[:depth]))
            if item["first_seen_at"] is None or folder.first_seen_at < item["first_seen_at"]:
                item["first_seen_at"] = folder.first_seen_at
            if item["last_seen_at"] is None or folder.last_seen_at > item["last_seen_at"]:
                item["last_seen_at"] = folder.last_seen_at

    for file in files:
        parent_parts = [part for part in PurePosixPath(file.parent_path).parts if part not in {"/", ""}]
        for depth in range(1, len(parent_parts) + 1):
            path = "/".join(parent_parts[:depth])
            item = ensure(path)
            item["files"] += file.live_files
            item["size"] += file.live_size
            if item["first_seen_at"] is None or file.first_seen_at < item["first_seen_at"]:
                item["first_seen_at"] = file.first_seen_at
            if item["last_seen_at"] is None or file.last_seen_at > item["last_seen_at"]:
                item["last_seen_at"] = file.last_seen_at

    for path, event_type, count in event_counts:
        relative = relative_to_watch(path, watch.root_path)
        if relative is None:
            continue
        parent_parts = [part for part in PurePosixPath(relative).parent.parts if part not in {"/", ".", ""}]
        for depth in range(1, len(parent_parts) + 1):
            item = folders.get("/".join(parent_parts[:depth]))
            if item is None:
                continue
            field = "updates" if event_type == EventType.UPDATED else "deleted"
            item[field] += count
    return sorted(folders.values(), key=lambda item: (item["path"].casefold(), item["depth"]))


@router.get("/{watch_id}/summary")
async def watch_summary(
    watch_id: int,
    current_user: str = Depends(require_observer),
    session: AsyncSession = Depends(get_session),
):
    watch = await _watch(session, watch_id, current_user)
    start_today = start_of_local_day_utc()
    files, received_today, deleted = (
        await session.execute(
            select(
                func.count().filter(FileRecord.exists.is_(True)),
                func.count().filter(FileRecord.first_seen_at >= start_today),
                func.count().filter(FileRecord.exists.is_(False)),
            ).where(FileRecord.watch_id == watch_id)
        )
    ).one()
    updated_today = await session.scalar(
        select(func.count()).where(
            ActivityEvent.watch_id == watch_id,
            ActivityEvent.event_type == EventType.UPDATED,
            ActivityEvent.event_time >= start_today,
        )
    )
    recent_files = await session.scalars(
        select(FileRecord).where(FileRecord.watch_id == watch_id)
        .order_by(FileRecord.first_seen_at.desc(), FileRecord.id.desc()).limit(10)
    )
    last_run = await session.scalar(
        select(ReconciliationRun)
        .where(ReconciliationRun.watch_id == watch_id)
        .order_by(ReconciliationRun.started_at.desc(), ReconciliationRun.id.desc())
        .limit(1)
    )
    last_failed_run = await session.scalar(
        select(ReconciliationRun)
        .where(ReconciliationRun.watch_id == watch_id, ReconciliationRun.error != "")
        .order_by(ReconciliationRun.started_at.desc(), ReconciliationRun.id.desc())
        .limit(1)
    )
    last_error_resolved = last_failed_run is not None and await session.scalar(
        select(
            select(ReconciliationRun.id).where(
                ReconciliationRun.watch_id == watch_id,
                ReconciliationRun.id > last_failed_run.id,
                ReconciliationRun.status == "completed",
            ).exists()
        )
    )
    folder_stats = await build_folder_stats(session, watch)
    for folder in folder_stats:
        folder["first_seen_at"] = api_datetime(folder["first_seen_at"])
        folder["last_seen_at"] = api_datetime(folder["last_seen_at"])
    return {
        "watch": watch_dict(watch),
        "metrics": {
            "files": files,
            "received_today": received_today,
            "updated_today": updated_today,
            "deleted": deleted,
            "folders": len(folder_stats),
        },
        "recent_files": [file_dict(item) for item in recent_files],
        "tree": folder_stats,
        "last_reconciliation": (
            {
                "status": last_run.status,
                "started_at": api_datetime(last_run.started_at),
                "finished_at": api_datetime(last_run.finished_at),
                "checked": last_run.checked_count,
                "created": last_run.created_count,
                "changed": last_run.updated_count,
                "missing": last_run.missing_count,
                "duration_seconds": (
                    round((_aware(last_run.finished_at) - _aware(last_run.started_at)).total_seconds(), 3)
                    if last_run.finished_at
                    else None
                ),
                "discrepancies": last_run.created_count + last_run.updated_count + last_run.missing_count,
                "error": last_run.error,
            }
            if last_run
            else None
        ),
        "diagnostics": {
            "last_webhook_at": api_datetime(watch.last_webhook_at),
            "reconciliation_duration_seconds": (
                round((_aware(last_run.finished_at) - _aware(last_run.started_at)).total_seconds(), 3)
                if last_run and last_run.finished_at
                else None
            ),
            "discrepancies": (
                last_run.created_count + last_run.updated_count + last_run.missing_count if last_run else None
            ),
            "last_error": last_failed_run.error if last_failed_run else "",
            "last_error_at": api_datetime(last_failed_run.finished_at) if last_failed_run else None,
            "last_error_resolved": bool(last_error_resolved),
        },
        "reconcile_interval_minutes": get_settings().reconcile_interval_minutes,
    }


@router.get("/{watch_id}/files")
async def watch_files(
    watch_id: int,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
    search: str = "",
    extension: str = "",
    file_status: str = Query(default="", alias="status", pattern="^(|new|updated|deleted)$"),
    path_prefix: str = "",
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    current_user: str = Depends(require_observer),
    session: AsyncSession = Depends(get_session),
):
    await _watch(session, watch_id, current_user)
    conditions = [FileRecord.watch_id == watch_id]
    if search:
        token = f"%{search.strip()}%"
        conditions.append(or_(FileRecord.filename.ilike(token), FileRecord.full_path.ilike(token)))
    if extension:
        conditions.append(FileRecord.extension == extension.strip().lstrip(".").lower())
    if file_status == "deleted":
        conditions.append(FileRecord.exists.is_(False))
    elif file_status == "updated":
        conditions.extend((FileRecord.exists.is_(True), FileRecord.revision_count > 0))
    elif file_status == "new":
        conditions.extend((FileRecord.exists.is_(True), FileRecord.revision_count == 0))
    if path_prefix:
        prefix = path_prefix.strip().strip("/")
        escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        conditions.append(
            or_(
                FileRecord.relative_path.ilike(escaped, escape="\\"),
                FileRecord.relative_path.ilike(f"{escaped}/%", escape="\\"),
            )
        )
    if date_from:
        conditions.append(FileRecord.first_seen_at >= date_from)
    if date_to:
        conditions.append(FileRecord.first_seen_at <= date_to)
    total = await session.scalar(select(func.count(FileRecord.id)).where(*conditions)) or 0
    pages = max(1, math.ceil(total / page_size))
    page = min(page, pages)
    items = list(
        (
            await session.scalars(
                select(FileRecord)
                .where(*conditions)
                .order_by(FileRecord.first_seen_at.desc(), FileRecord.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).all()
    )
    return {
        "items": [file_dict(item) for item in items],
        "page": page,
        "page_size": page_size,
        "total": total,
        "pages": pages,
    }


@router.get("/{watch_id}/events")
async def watch_events(
    watch_id: int,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
    search: str = "",
    event_type: EventType | None = None,
    path_prefix: str = "",
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    current_user: str = Depends(require_observer),
    session: AsyncSession = Depends(get_session),
):
    watch = await _watch(session, watch_id, current_user)
    conditions = [ActivityEvent.watch_id == watch_id]
    if search:
        token = f"%{search.strip()}%"
        conditions.append(or_(ActivityEvent.path.ilike(token), ActivityEvent.old_path.ilike(token)))
    if event_type:
        conditions.append(ActivityEvent.event_type == event_type)
    if path_prefix:
        full_prefix = watch.root_path.rstrip("/") + "/" + path_prefix.strip().strip("/")
        escaped = full_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        conditions.append(
            or_(
                ActivityEvent.path.ilike(escaped, escape="\\"),
                ActivityEvent.path.ilike(f"{escaped}/%", escape="\\"),
                ActivityEvent.old_path.ilike(escaped, escape="\\"),
                ActivityEvent.old_path.ilike(f"{escaped}/%", escape="\\"),
            )
        )
    if date_from:
        conditions.append(ActivityEvent.event_time >= date_from)
    if date_to:
        conditions.append(ActivityEvent.event_time <= date_to)
    total = await session.scalar(select(func.count(ActivityEvent.id)).where(*conditions)) or 0
    items = list(
        (
            await session.scalars(
                select(ActivityEvent)
                .where(*conditions)
                .options(selectinload(ActivityEvent.file))
                .order_by(ActivityEvent.event_time.desc(), ActivityEvent.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).all()
    )
    return {
        "items": [event_dict(item) for item in items],
        "page": page,
        "page_size": page_size,
        "total": total,
        "pages": max(1, math.ceil(total / page_size)),
    }


@router.get("/{watch_id}/tree")
async def watch_tree(
    watch_id: int,
    current_user: str = Depends(require_observer),
    session: AsyncSession = Depends(get_session),
):
    watch = await _watch(session, watch_id, current_user)
    return {"items": await build_folder_stats(session, watch)}


@router.post("/{watch_id}/reconcile")
async def manual_reconcile(watch_id: int, admin_user: str = Depends(require_admin)):
    try:
        result = await reconcile_watch(watch_id, triggered_by=admin_user)
        if result["status"] == "skipped":
            raise HTTPException(status_code=409, detail=str(result["reason"]))
        return result
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Сверка не выполнена: {exc}") from exc
