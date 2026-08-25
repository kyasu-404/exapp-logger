from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from file_control.db import SessionFactory
from file_control.models import (
    Campaign,
    CampaignOrganization,
    CampaignStatus,
    Dimension,
    DimensionValue,
    EventType,
    FileEvent,
    FileRecord,
    Organization,
    Requirement,
    StructureMode,
)
from file_control.services import events as event_service
from file_control.services.events import NodeSnapshot, normalize_event, process_event_payload
from file_control.services.imports import requirement_identity
from file_control.services.progress import build_progress, build_summary, filter_progress
from file_control.services.reporting import build_csv, build_xlsx, remove_report


async def seed_campaign() -> int:
    async with SessionFactory() as session:
        organization = Organization(
            code="ORG-1",
            name="ООО Ромашка",
            short_name="Ромашка",
            folder_name="ООО Ромашка",
        )
        campaign = Campaign(
            name="Квартальный сбор",
            year=2026,
            root_path="/Сбор/2026",
            storage_user_id="collector",
            created_by="admin",
            status=CampaignStatus.ACTIVE,
            structure_mode=StructureMode.STRICT,
        )
        session.add_all([organization, campaign])
        await session.flush()
        session.add(CampaignOrganization(campaign_id=campaign.id, organization_id=organization.id))
        dimension = Dimension(
            campaign_id=campaign.id,
            position=0,
            key="period",
            name="Период",
            fixed=True,
        )
        session.add(dimension)
        await session.flush()
        session.add(
            DimensionValue(
                dimension_id=dimension.id,
                value="1 квартал",
                display_name="1 квартал",
            )
        )
        dimensions = {"period": "1 квартал"}
        session.add(
            Requirement(
                campaign_id=campaign.id,
                organization_id=organization.id,
                identity_key=requirement_identity(dimensions),
                dimensions=dimensions,
                required_file_count=1,
            )
        )
        await session.commit()
        return campaign.id


def legacy_event(subtype: str, etag: str = "v1") -> dict:
    return {
        "event_type": "node_event",
        "event_subtype": subtype,
        "event_data": {
            "target": {
                "userId": "collector",
                "directory": "/Сбор/2026/ООО Ромашка/1 квартал",
                "name": "report.xlsx",
                "fileId": "9001",
                "etag": etag,
                "mime": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "size": 1024,
                "mtime": "2020-01-01T00:00:00Z",
                "fileType": "file",
            }
        },
    }


@pytest.mark.asyncio
async def test_current_webhook_envelope(monkeypatch) -> None:
    async def fake_enrichment(snapshot: NodeSnapshot) -> NodeSnapshot:
        snapshot.etag = "webhook-etag"
        snapshot.size = 2048
        return snapshot

    monkeypatch.setattr(event_service, "enrich_snapshot", fake_enrichment)
    event = await normalize_event(
        {
            "event": {
                "class": "OCP\\Files\\Events\\Node\\NodeCreatedEvent",
                "node": {"id": 42, "path": "/collector/files/Сбор/2026/ООО Ромашка/1 квартал/report.xlsx"},
            },
            "user": {"uid": "collector", "displayName": "Сборщик"},
            "time": 1787600000,
        }
    )
    assert event.event_type == EventType.CREATED
    assert event.target.owner_user_id == "collector"
    assert event.target.path == "/Сбор/2026/ООО Ромашка/1 квартал/report.xlsx"
    assert event.target.file_id == "42"
    assert event.target.etag == "webhook-etag"


@pytest.mark.asyncio
async def test_event_idempotency_revision_delete_and_reports() -> None:
    campaign_id = await seed_campaign()

    created = await process_event_payload(legacy_event("NodeCreatedEvent"))
    duplicate = await process_event_payload(legacy_event("NodeCreatedEvent"))
    written = await process_event_payload(legacy_event("NodeWrittenEvent", "v2"))

    assert created["changed"] == 1
    assert duplicate["changed"] == 0
    assert written["changed"] == 1

    async with SessionFactory() as session:
        file = await session.scalar(select(FileRecord).options(selectinload(FileRecord.dimension_values)))
        assert file is not None
        assert file.revision_count == 1
        assert file.first_received_at.date() == datetime.now(UTC).date()
        assert file.node_mtime.year == 2020
        assert file.dimension_values[0].raw_value == "1 квартал"
        assert await session.scalar(select(func.count(FileEvent.id))) == 2

        progress = await build_progress(session, campaign_id)
        assert progress["percent"] == 100.0
        assert progress["organizations"][0]["status"] == "submitted"
        filtered = filter_progress(
            progress,
            status_filter="submitted",
            dimensions={"period": "1 квартал"},
        )
        assert filtered["required"] == 1
        assert filtered["organizations"][0]["requirements"][0]["dimensions"]["period"] == "1 квартал"
        assert filter_progress(progress, dimensions={"period": "2 квартал"})["organizations"] == []
        summary = await build_summary(session, campaign_id)
        assert summary["files"]["total"] == 1

        xlsx_path, xlsx_name = await build_xlsx(session, campaign_id)
        csv_path, csv_name = await build_csv(session, campaign_id, "files")
        filtered_csv_path, _ = await build_csv(
            session,
            campaign_id,
            "files",
            {"dimensions": {"period": "2 квартал"}},
        )
        assert xlsx_path.stat().st_size > 0 and xlsx_name.endswith(".xlsx")
        assert csv_path.read_bytes().startswith(b"\xef\xbb\xbf") and csv_name.endswith(".csv")
        assert len(filtered_csv_path.read_text(encoding="utf-8-sig").splitlines()) == 1
        remove_report(xlsx_path)
        remove_report(csv_path)
        remove_report(filtered_csv_path)

    deleted = await process_event_payload(legacy_event("NodeDeletedEvent", "v2"))
    assert deleted["changed"] == 1
    async with SessionFactory() as session:
        file = await session.scalar(select(FileRecord))
        assert file.deleted is True
        assert await session.scalar(select(func.count(FileEvent.id))) == 3
