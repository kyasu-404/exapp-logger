from __future__ import annotations

from fastapi.testclient import TestClient

from file_control.main import APP


def test_health_and_campaign_workflow() -> None:
    with TestClient(APP) as client:
        assert client.get("/heartbeat").json() == {"status": "ok"}

        organization_response = client.post(
            "/api/organizations",
            json={
                "code": "ORG-API",
                "name": "Тестовая организация",
                "short_name": "Тест",
                "folder_name": "Тестовая организация",
            },
        )
        assert organization_response.status_code == 201
        organization_id = organization_response.json()["id"]

        campaign_response = client.post(
            "/api/campaigns",
            json={
                "name": "API кампания",
                "year": 2026,
                "root_path": "/Сбор/API",
                "storage_user_id": "collector",
                "organization_ids": [organization_id],
                "dimensions": [
                    {
                        "key": "period",
                        "name": "Период",
                        "values": [{"value": "Август"}],
                    }
                ],
            },
        )
        assert campaign_response.status_code == 201
        campaign = campaign_response.json()
        assert campaign["organization_ids"] == [organization_id]
        assert campaign["dimensions"][0]["values"][0]["value"] == "Август"

        listed = client.get("/api/campaigns").json()
        assert [item["name"] for item in listed] == ["API кампания"]

        xlsx = client.get(
            "/api/reports/xlsx",
            params={"campaign_id": campaign["id"], "status": "not_submitted"},
        )
        assert xlsx.status_code == 200
        assert xlsx.content.startswith(b"PK")

        csv = client.get(
            "/api/reports/csv",
            params={"campaign_id": campaign["id"], "table": "progress", "status": "not_submitted"},
        )
        assert csv.status_code == 200
        assert csv.content.startswith(b"\xef\xbb\xbf")
