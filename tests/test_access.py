from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from file_control import security
from file_control.main import APP
from file_control.security import watch_is_accessible


def test_watch_access_uses_owner_or_assigned_group() -> None:
    watch = SimpleNamespace(owner_user_id="owner", group_ids=["ЦИО", "ЦУОКО"])

    assert watch_is_accessible(watch, "owner", set(), False)
    assert watch_is_accessible(watch, "member", {"ЦИО"}, False)
    assert not watch_is_accessible(watch, "member", {"ИМЦ"}, False)
    assert watch_is_accessible(watch, "administrator", set(), True)


def test_group_member_sees_only_assigned_watches(monkeypatch) -> None:
    with TestClient(APP) as client:
        accessible = client.post(
            "/api/watches",
            json={
                "name": "Олимпиады",
                "root_path": "/8. Олимпиады",
                "owner_user_id": "collector",
                "scope_type": "team",
                "group_ids": ["ЦИО"],
            },
        ).json()
        hidden = client.post(
            "/api/watches",
            json={
                "name": "Скрытое",
                "root_path": "/Скрытое",
                "owner_user_id": "collector",
                "scope_type": "team",
                "group_ids": ["Другая группа"],
            },
        ).json()

        async def member_groups(_user_id: str) -> set[str]:
            return {"ЦИО"}

        monkeypatch.setattr(security, "user_groups", member_groups)
        headers = {"X-Debug-User": "group-member"}

        access = client.get("/api/access", headers=headers)
        assert access.status_code == 200
        assert access.json() == {"is_admin": False}

        watches = client.get("/api/watches", headers=headers)
        assert watches.status_code == 200
        assert [item["id"] for item in watches.json()] == [accessible["id"]]

        dashboard = client.get("/api/dashboard", headers=headers)
        assert dashboard.status_code == 200
        assert [item["id"] for item in dashboard.json()["watches"]] == [accessible["id"]]

        visible_summary = client.get(f"/api/watches/{accessible['id']}/summary", headers=headers)
        assert visible_summary.status_code == 200
        hidden_summary = client.get(f"/api/watches/{hidden['id']}/summary", headers=headers)
        assert hidden_summary.status_code == 403

        report = client.get("/api/reports/csv", params={"watch_id": accessible["id"]}, headers=headers)
        assert report.status_code == 200
