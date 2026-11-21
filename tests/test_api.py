from __future__ import annotations

from fastapi.testclient import TestClient
from openpyxl import load_workbook

from file_control.main import APP


def test_health_watch_and_reports() -> None:
    with TestClient(APP) as client:
        assert client.get("/heartbeat").json() == {"status": "ok"}

        folder_info = client.get("/api/folder-info", params={"path": "/Личная папка"})
        assert folder_info.status_code == 200
        assert folder_info.json()["scope_type"] == "personal"
        assert folder_info.json()["available_groups"] == [{"id": "admin", "name": "admin", "users": 1}]

        response = client.post(
            "/api/watches",
            json={
                "name": "Олимпиада 2026",
                "root_path": "/Олимпиада 2026",
                "owner_user_id": "collector",
                "scope_type": "team",
                "group_ids": ["schools"],
                "started_at": "2026-09-01T00:00:00Z",
                "ended_at": "2026-11-01T23:59:59Z",
                "status": "active",
            },
        )
        assert response.status_code == 201
        watch = response.json()
        assert watch["root_path"] == "/Олимпиада 2026"
        assert watch["scope_type"] == "team"
        assert watch["group_ids"] == ["schools"]
        assert "organization_ids" not in watch

        listed = client.get("/api/watches").json()
        assert [item["name"] for item in listed] == ["Олимпиада 2026"]
        assert listed[0]["counts"] == {"files": 0, "deleted": 0}

        summary = client.get(f"/api/watches/{watch['id']}/summary").json()
        assert summary["metrics"] == {
            "files": 0,
            "received_today": 0,
            "updated_today": 0,
            "deleted": 0,
            "folders": 0,
        }
        assert summary["diagnostics"] == {
            "last_webhook_at": None,
            "reconciliation_duration_seconds": None,
            "discrepancies": None,
            "last_error": "",
            "last_error_at": None,
            "last_error_resolved": False,
        }

        xlsx = client.get("/api/reports/xlsx", params={"watch_id": watch["id"]})
        assert xlsx.status_code == 200
        assert xlsx.content.startswith(b"PK")
        assert xlsx.headers["cache-control"].startswith("no-store")
        workbook = load_workbook(filename=__import__("io").BytesIO(xlsx.content), read_only=True)
        assert workbook.sheetnames == ["Файлы", "Активность", "По папкам", "Пояснения"]
        headers = next(workbook["Файлы"].iter_rows(values_only=True))
        assert "Дата файла (mtime)" in headers
        assert "Размер" in headers
        assert "Размер, байт" not in headers

        csv = client.get("/api/reports/csv", params={"watch_id": watch["id"], "table": "files"})
        assert csv.status_code == 200
        assert csv.content.startswith(b"\xef\xbb\xbf")
        assert csv.headers["cache-control"].startswith("no-store")

        changes = client.get(
            "/api/reports/changes/xlsx",
            params={
                "watch_id": watch["id"],
                "date_from": "2026-09-01T00:00:00+03:00",
                "date_to": "2026-09-30T23:59:59+03:00",
            },
        )
        assert changes.status_code == 200
        change_book = load_workbook(filename=__import__("io").BytesIO(changes.content), read_only=True)
        assert change_book.sheetnames == ["Итоги", "Изменения"]

        schedule = client.put(
            f"/api/reports/schedule/{watch['id']}",
            json={
                "enabled": True,
                "cadence": "weekly",
                "local_time": "09:30",
                "weekday": 2,
                "day_of_month": 1,
                "destination_path": "/Автоматические отчёты",
            },
        )
        assert schedule.status_code == 200
        assert schedule.json()["enabled"] is True
        assert schedule.json()["next_run_at"].endswith("+03:00")
        assert client.get(f"/api/reports/schedule/{watch['id']}").json()["cadence"] == "weekly"


def test_invalid_period_and_root() -> None:
    with TestClient(APP) as client:
        invalid_root = client.post("/api/watches", json={"name": "Корень", "root_path": "/"})
        assert invalid_root.status_code == 422
        invalid_period = client.post(
            "/api/watches",
            json={
                "name": "Период",
                "root_path": "/Папка",
                "started_at": "2026-11-02T00:00:00Z",
                "ended_at": "2026-11-01T00:00:00Z",
            },
        )
        assert invalid_period.status_code == 422
