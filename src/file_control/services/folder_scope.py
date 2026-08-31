from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from ..config import in_nextcloud_runtime
from ..db import SessionFactory
from ..models import Watch, WatchStatus

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class FolderScope:
    scope_type: str
    group_ids: list[str]
    groups: list[dict[str, Any]]
    team_folder_id: int | None = None
    mount_point: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "scope_type": self.scope_type,
            "group_ids": self.group_ids,
            "groups": self.groups,
            "team_folder_id": self.team_folder_id,
            "mount_point": self.mount_point,
        }


def _normalized_path(path: str) -> str:
    return "/" + str(path).replace("\\", "/").strip("/")


def _folder_values(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        values = payload.values()
    elif isinstance(payload, list):
        values = payload
    else:
        values = []
    return [item for item in values if isinstance(item, dict)]


def classify_folder(path: str, team_folders: Any) -> FolderScope:
    selected = _normalized_path(path)
    matches: list[tuple[int, dict[str, Any], str]] = []
    for item in _folder_values(team_folders):
        mount_point = _normalized_path(item.get("mount_point", ""))
        if mount_point != "/" and (selected == mount_point or selected.startswith(f"{mount_point}/")):
            matches.append((len(mount_point), item, mount_point))
    if not matches:
        return FolderScope(scope_type="personal", group_ids=[], groups=[])

    _, folder, mount_point = max(matches, key=lambda value: value[0])
    details = folder.get("group_details") if isinstance(folder.get("group_details"), dict) else {}
    raw_groups = folder.get("groups") if isinstance(folder.get("groups"), dict) else {}
    group_ids = sorted({str(item) for item in raw_groups})
    groups = []
    for group_id in group_ids:
        detail = details.get(group_id) if isinstance(details.get(group_id), dict) else {}
        groups.append(
            {
                "id": group_id,
                "name": str(detail.get("displayName") or detail.get("display_name") or group_id),
                "permissions": int(detail.get("permissions") or raw_groups.get(group_id) or 0),
            }
        )
    folder_id = folder.get("id")
    return FolderScope(
        scope_type="team",
        group_ids=group_ids,
        groups=groups,
        team_folder_id=int(folder_id) if folder_id is not None else None,
        mount_point=mount_point,
    )


async def load_team_folders(user_id: str) -> Any:
    if not in_nextcloud_runtime():
        return {}
    from nc_py_api import AsyncNextcloudApp

    return await AsyncNextcloudApp(user=user_id).ocs(
        "GET", "/index.php/apps/groupfolders/folders"
    )


async def inspect_folder(path: str, user_id: str) -> FolderScope:
    return classify_folder(path, await load_team_folders(user_id))


async def list_available_groups(user_id: str) -> list[dict[str, Any]]:
    if not in_nextcloud_runtime():
        return [{"id": "admin", "name": "admin", "users": 1}]

    from nc_py_api import AsyncNextcloudApp

    nc = AsyncNextcloudApp(user=user_id)
    try:
        raw_groups = await nc.ocs(
            "GET", "/index.php/apps/groupfolders/delegation/groups"
        )
    except Exception:
        details = await nc.users_groups.get_details(limit=500)
        return sorted(
            [
                {
                    "id": item.group_id,
                    "name": item.display_name or item.group_id,
                    "users": item.user_count,
                }
                for item in details
                if not item.disabled
            ],
            key=lambda item: item["name"].casefold(),
        )

    user_counts: dict[str, int] = {}
    try:
        details = await nc.users_groups.get_details(limit=500)
        user_counts = {item.group_id: item.user_count for item in details if not item.disabled}
    except Exception:
        LOGGER.debug("Не удалось получить количество пользователей в группах", exc_info=True)

    if isinstance(raw_groups, dict):
        values = list(raw_groups.values())
    elif isinstance(raw_groups, list):
        values = raw_groups
    else:
        values = []
    result = []
    for item in values:
        if not isinstance(item, dict):
            continue
        group_id = str(item.get("gid") or item.get("id") or "").strip()
        if not group_id:
            continue
        result.append(
            {
                "id": group_id,
                "name": str(item.get("displayName") or item.get("display_name") or group_id),
                "users": user_counts.get(group_id),
            }
        )
    return sorted(result, key=lambda item: item["name"].casefold())


async def resolve_watch_scope(
    path: str,
    user_id: str,
    requested_scope: str,
    requested_groups: list[str] | None,
) -> tuple[str, list[str]]:
    normalized_groups = sorted({item.strip() for item in (requested_groups or []) if item.strip()})
    if not in_nextcloud_runtime():
        return requested_scope, normalized_groups

    scope = await inspect_folder(path, user_id)
    if scope.scope_type == "team":
        return "team", scope.group_ids

    if normalized_groups:
        available = {item["id"] for item in await list_available_groups(user_id)}
        unknown = sorted(set(normalized_groups) - available)
        if unknown:
            raise ValueError(f"Неизвестные группы Nextcloud: {', '.join(unknown)}")
    return "personal", normalized_groups


async def synchronize_active_watch_scopes() -> int:
    if not in_nextcloud_runtime():
        return 0

    changed = 0
    async with SessionFactory() as session:
        watches = list(
            (
                await session.scalars(
                    select(Watch).where(Watch.status != WatchStatus.ARCHIVED)
                )
            ).all()
        )
        catalogs: dict[str, Any] = {}
        for watch in watches:
            if watch.owner_user_id not in catalogs:
                try:
                    catalogs[watch.owner_user_id] = await load_team_folders(watch.owner_user_id)
                except Exception:
                    LOGGER.warning(
                        "Не удалось определить командные папки пользователя %s",
                        watch.owner_user_id,
                        exc_info=True,
                    )
                    catalogs[watch.owner_user_id] = None
            catalog = catalogs[watch.owner_user_id]
            if catalog is None:
                continue
            actual = classify_folder(watch.root_path, catalog)
            actual_groups = actual.group_ids if actual.scope_type == "team" else watch.group_ids
            if watch.scope_type != actual.scope_type or watch.group_ids != actual_groups:
                watch.scope_type = actual.scope_type
                watch.group_ids = actual_groups
                changed += 1
        if changed:
            await session.commit()
    return changed
