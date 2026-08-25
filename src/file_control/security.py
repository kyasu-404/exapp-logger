from __future__ import annotations

import hashlib
import logging
import os

from fastapi import HTTPException, Request, status

from .config import get_settings, in_nextcloud_runtime

LOGGER = logging.getLogger(__name__)


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


async def _user_groups(user_id: str) -> set[str]:
    if not in_nextcloud_runtime():
        return {"admin", "file_control_admins", "file_control_observers"}
    try:
        from nc_py_api import AsyncNextcloudApp

        nc = AsyncNextcloudApp(user=user_id)
        info = await nc.users.get_user()
        return set(info.groups)
    except Exception as exc:
        LOGGER.warning("Не удалось определить группы пользователя %s: %s", user_id, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Не удалось проверить права пользователя через Nextcloud",
        ) from exc


async def require_observer(request: Request) -> str:
    user_id = current_user_id(request)
    settings = get_settings()
    groups = await _user_groups(user_id)
    allowed = settings.admin_group_set | settings.observer_group_set
    if not groups.intersection(allowed):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Нет доступа к контролю файлов")
    return user_id


async def require_admin(request: Request) -> str:
    user_id = current_user_id(request)
    settings = get_settings()
    groups = await _user_groups(user_id)
    if not groups.intersection(settings.admin_group_set):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Требуются права администратора")
    return user_id


def system_or_authenticated_user(request: Request) -> str:
    """Return the signed AppAPI user, allowing empty system context for event delivery."""
    if not in_nextcloud_runtime():
        return request.headers.get("X-Debug-User", "system")
    return str(request.scope.get("username") or "system")
