from __future__ import annotations

import hashlib
import logging
import os
import time
from collections.abc import Iterable

from fastapi import HTTPException, Request, status

from .config import get_settings, in_nextcloud_runtime

LOGGER = logging.getLogger(__name__)
_GROUP_CACHE: dict[str, tuple[float, set[str]]] = {}


def webhook_token() -> str:
    """Derive a purpose-specific secret without exposing the AppAPI application secret."""
    app_secret = os.getenv("APP_SECRET", "")
    app_id = os.getenv("APP_ID", "file_control")
    if not app_secret:
        return ""
    return hashlib.sha256(f"{app_id}:file-control-webhook:{app_secret}".encode()).hexdigest()


def current_user_id(request: Request) -> str:
    user_id = str(request.scope.get("username") or "")
    if not user_id and not in_nextcloud_runtime():
        user_id = request.headers.get("X-Debug-User", "admin")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Требуется вход в Nextcloud")
    return user_id


async def user_groups(user_id: str) -> set[str]:
    if not in_nextcloud_runtime():
        return {"admin", "file_control_admins", "file_control_observers"}
    cached = _GROUP_CACHE.get(user_id)
    now = time.monotonic()
    if cached and now - cached[0] < 300:
        return cached[1]
    try:
        from nc_py_api import AsyncNextcloudApp

        nc = AsyncNextcloudApp(user=user_id)
        info = await nc.users.get_user()
        groups = set(info.groups)
        _GROUP_CACHE[user_id] = (now, groups)
        return groups
    except Exception as exc:
        LOGGER.warning("Не удалось определить группы пользователя %s: %s", user_id, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Не удалось проверить права пользователя через Nextcloud",
        ) from exc


def watch_is_accessible(watch, user_id: str, groups: set[str], privileged: bool) -> bool:
    return bool(
        privileged
        or watch.owner_user_id == user_id
        or set(watch.group_ids or []).intersection(groups)
    )


async def access_context(user_id: str) -> tuple[set[str], bool]:
    groups = await user_groups(user_id)
    settings = get_settings()
    privileged = bool(groups.intersection(settings.admin_group_set | settings.observer_group_set))
    return groups, privileged


async def filter_accessible_watches(user_id: str, watches: Iterable) -> list:
    groups, privileged = await access_context(user_id)
    return [watch for watch in watches if watch_is_accessible(watch, user_id, groups, privileged)]


async def ensure_watch_access(user_id: str, watch) -> None:
    groups, privileged = await access_context(user_id)
    if not watch_is_accessible(watch, user_id, groups, privileged):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Нет доступа к наблюдению")


async def is_admin_user(user_id: str) -> bool:
    groups = await user_groups(user_id)
    return bool(groups.intersection(get_settings().admin_group_set))


async def require_observer(request: Request) -> str:
    user_id = current_user_id(request)
    groups, privileged = await access_context(user_id)
    if privileged:
        return user_id

    from sqlalchemy import select

    from .db import SessionFactory
    from .models import Watch, WatchStatus

    async with SessionFactory() as session:
        watches = list(
            (
                await session.scalars(
                    select(Watch).where(Watch.status != WatchStatus.ARCHIVED)
                )
            ).all()
        )
    if any(watch_is_accessible(watch, user_id, groups, False) for watch in watches):
        return user_id
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Нет доступа к контролю файлов")


async def require_admin(request: Request) -> str:
    user_id = current_user_id(request)
    if not await is_admin_user(user_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Требуются права администратора")
    return user_id


def system_or_authenticated_user(request: Request) -> str:
    """Return the signed AppAPI user, allowing empty system context for event delivery."""
    if not in_nextcloud_runtime():
        return request.headers.get("X-Debug-User", "system")
    return str(request.scope.get("username") or "system")
