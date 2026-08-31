from __future__ import annotations

import pytest

from file_control.services import folder_scope
from file_control.services.folder_scope import FolderScope, classify_folder

TEAM_FOLDERS = {
    "10": {
        "id": 10,
        "mount_point": "8. Олимпиады",
        "groups": {"ЦИО": 31, "ЦУОКО": 31},
        "group_details": {
            "ЦИО": {"displayName": "ЦИО", "permissions": 31, "type": "group"},
            "ЦУОКО": {"displayName": "ЦУОКО", "permissions": 31, "type": "group"},
        },
    }
}


def test_team_folder_and_descendants_are_detected() -> None:
    root = classify_folder("/8. Олимпиады", TEAM_FOLDERS)
    child = classify_folder("/8. Олимпиады/Школа 525/Математика", TEAM_FOLDERS)

    assert root.scope_type == "team"
    assert child.scope_type == "team"
    assert root.team_folder_id == 10
    assert root.group_ids == ["ЦИО", "ЦУОКО"]
    assert [item["name"] for item in root.groups] == ["ЦИО", "ЦУОКО"]


def test_similarly_named_personal_folder_is_not_team_folder() -> None:
    result = classify_folder("/8. Олимпиады личная", TEAM_FOLDERS)

    assert result.scope_type == "personal"
    assert result.group_ids == []


@pytest.mark.asyncio
async def test_server_overrides_manual_type_for_team_folder(monkeypatch) -> None:
    async def detected(_path: str, _user_id: str) -> FolderScope:
        return FolderScope(
            scope_type="team",
            group_ids=["ЦИО", "ЦУОКО"],
            groups=[],
            team_folder_id=10,
            mount_point="/8. Олимпиады",
        )

    monkeypatch.setattr(folder_scope, "in_nextcloud_runtime", lambda: True)
    monkeypatch.setattr(folder_scope, "inspect_folder", detected)

    actual = await folder_scope.resolve_watch_scope(
        "/8. Олимпиады",
        "admin",
        requested_scope="personal",
        requested_groups=["admin"],
    )

    assert actual == ("team", ["ЦИО", "ЦУОКО"])


@pytest.mark.asyncio
async def test_server_overrides_manual_type_for_personal_folder(monkeypatch) -> None:
    async def detected(_path: str, _user_id: str) -> FolderScope:
        return FolderScope(scope_type="personal", group_ids=[], groups=[])

    monkeypatch.setattr(folder_scope, "in_nextcloud_runtime", lambda: True)
    monkeypatch.setattr(folder_scope, "inspect_folder", detected)

    actual = await folder_scope.resolve_watch_scope(
        "/Личная папка",
        "admin",
        requested_scope="team",
        requested_groups=[],
    )

    assert actual == ("personal", [])
