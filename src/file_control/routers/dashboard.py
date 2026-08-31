from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings, in_nextcloud_runtime
from ..db import get_session
from ..models import FileRecord, Watch, WatchStatus
from ..schemas import normalize_path
from ..security import filter_accessible_watches, is_admin_user, require_admin, require_observer
from ..serialization import file_dict, watch_dict
from ..services.folder_scope import inspect_folder, list_available_groups
from ..time_utils import start_of_local_day_utc

router = APIRouter(prefix="/api", tags=["dashboard"])


@router.get("/dashboard")
async def dashboard(
    current_user: str = Depends(require_observer),
    session: AsyncSession = Depends(get_session),
):
    watches = list(
        (
            await session.scalars(
                select(Watch).where(Watch.status == WatchStatus.ACTIVE).order_by(Watch.ended_at, Watch.created_at)
            )
        ).all()
    )
    watches = await filter_accessible_watches(current_user, watches)
    watch_ids = [item.id for item in watches]
    start_today = start_of_local_day_utc()
    watch_condition = FileRecord.watch_id.in_(watch_ids)
    files_now = (
        await session.scalar(
            select(func.count(FileRecord.id)).where(watch_condition, FileRecord.exists.is_(True))
        )
        or 0
    )
    files_today = (
        await session.scalar(
            select(func.count(FileRecord.id)).where(watch_condition, FileRecord.first_seen_at >= start_today)
        )
        or 0
    )
    updated_today = (
        await session.scalar(
            select(func.count(FileRecord.id)).where(
                watch_condition,
                FileRecord.revision_count > 0,
                FileRecord.last_seen_at >= start_today,
            )
        )
        or 0
    )
    deleted = (
        await session.scalar(
            select(func.count(FileRecord.id)).where(watch_condition, FileRecord.exists.is_(False))
        )
        or 0
    )
    recent = list(
        (
            await session.scalars(
                select(FileRecord)
                .where(watch_condition)
                .order_by(FileRecord.first_seen_at.desc())
                .limit(10)
            )
        ).all()
    )
    return {
        "metrics": {
            "active_watches": len(watches),
            "files": files_now,
            "received_today": files_today,
            "updated_today": updated_today,
            "deleted": deleted,
        },
        "watches": [watch_dict(item) for item in watches],
        "recent_files": [file_dict(item) for item in recent],
    }


@router.get("/access")
async def access(current_user: str = Depends(require_observer)):
    return {"is_admin": await is_admin_user(current_user)}


@router.get("/folders")
async def folders(
    path: str = "",
    user_id: str | None = None,
    current_user: str = Depends(require_admin),
):
    if not in_nextcloud_runtime():
        return {"user_id": user_id or current_user, "path": path, "items": []}
    try:
        from nc_py_api import AsyncNextcloudApp

        owner = user_id or current_user
        nc = AsyncNextcloudApp(user=owner)
        nodes = await nc.files.listdir(path.strip("/"), depth=1)
        return {
            "user_id": owner,
            "path": path,
            "items": [
                {"name": node.name, "path": "/" + node.user_path.strip("/"), "file_id": node.info.fileid}
                for node in nodes
                if node.is_dir
            ],
        }
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Не удалось получить список папок: {exc}") from exc


@router.get("/groups")
async def groups(current_user: str = Depends(require_admin)):
    try:
        return {"items": await list_available_groups(current_user)}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Не удалось получить группы Nextcloud: {exc}") from exc


@router.get("/folder-info")
async def folder_info(path: str, current_user: str = Depends(require_admin)):
    try:
        normalized = normalize_path(path)
        result = (await inspect_folder(normalized, current_user)).as_dict()
        result["path"] = normalized
        result["available_groups"] = (
            await list_available_groups(current_user)
            if result["scope_type"] == "personal"
            else []
        )
        return result
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Не удалось определить тип папки через Nextcloud: {exc}",
        ) from exc


@router.get("/system/status")
async def system_status(
    _user: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    await session.scalar(select(func.count(Watch.id)))
    settings = get_settings()
    return {
        "status": "ok",
        "database": "connected",
        "nextcloud_runtime": in_nextcloud_runtime(),
        "reconcile_interval_minutes": settings.reconcile_interval_minutes,
        "admin_groups": sorted(settings.admin_group_set),
        "observer_groups": sorted(settings.observer_group_set),
    }
