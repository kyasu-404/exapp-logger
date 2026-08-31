from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from file_control.db import SessionFactory
from file_control.models import ActivityEvent, EventType, FileRecord, FolderRecord, Watch, WatchStatus
from file_control.services import events as event_service
from file_control.services import reconciliation as reconciliation_service
from file_control.services.events import (
    NodeSnapshot,
    normalize_event,
    process_event_payload,
    relative_prefix_matches,
    relative_to_watch,
)
from file_control.services.reconciliation import _event as reconciliation_event
from file_control.services.reconciliation import refresh_file_metadata
from file_control.services.reporting import build_changes_xlsx, build_xlsx, remove_report


async def seed_watch(
    *, owner_user_id: str = "collector", scope_type: str = "personal", group_ids: list[str] | None = None
) -> int:
    async with SessionFactory() as session:
        watch = Watch(
            name="Олимпиада 2026",
            root_path="/Олимпиада 2026",
            owner_user_id=owner_user_id,
            scope_type=scope_type,
            group_ids=group_ids or [],
            created_by="admin",
            status=WatchStatus.ACTIVE,
        )
        session.add(watch)
        await session.commit()
        return watch.id


def legacy_event(subtype: str, *, path: str, etag: str = "v1", source_path: str = "", file_type: str = "file") -> dict:
    def node(value: str) -> dict:
        directory, name = value.rsplit("/", 1)
        return {
            "userId": "collector",
            "directory": directory,
            "name": name,
            "fileId": "9001",
            "etag": etag,
            "mime": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "size": 1024,
            "mtime": "2020-01-01T00:00:00Z",
            "upload_time": "2026-09-10T10:00:00Z",
            "fileType": file_type,
        }

    data = {"target": node(path)}
    if source_path:
        data["source"] = node(source_path)
    return {"event_type": "node_event", "event_subtype": subtype, "event_data": data}


def test_path_boundary_has_no_structure_assumptions() -> None:
    assert (
        relative_to_watch(
            "/Олимпиада 2026/школа 525/математика/7/ещё/result.xlsx",
            "/Олимпиада 2026",
        )
        == "школа 525/математика/7/ещё/result.xlsx"
    )
    assert relative_to_watch("/Олимпиада 20260/file.xlsx", "/Олимпиада 2026") is None
    assert relative_prefix_matches("1/result.xlsx", "1") is True
    assert relative_prefix_matches("11/result.xlsx", "1") is False
    assert relative_prefix_matches("111/result.xlsx", "1") is False


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
                "node": {
                    "id": 42,
                    "path": "/collector/files/Олимпиада 2026/525/Математика/7/result.xlsx",
                },
            },
            "user": {"uid": "collector"},
            "time": 1789034400,
        }
    )
    assert event.event_type == EventType.CREATED
    assert event.target.path.endswith("/525/Математика/7/result.xlsx")
    assert event.target.etag == "webhook-etag"


@pytest.mark.asyncio
async def test_create_update_move_delete_and_dynamic_report() -> None:
    watch_id = await seed_watch()
    initial_path = "/Олимпиада 2026/525/Математика/7/ещё/result.xlsx"
    moved_path = "/Олимпиада 2026/525/Математика/8/result.xlsx"

    created = await process_event_payload(legacy_event("NodeCreatedEvent", path=initial_path))
    duplicate = await process_event_payload(legacy_event("NodeCreatedEvent", path=initial_path))
    updated = await process_event_payload(legacy_event("NodeWrittenEvent", path=initial_path, etag="v2"))
    moved = await process_event_payload(
        legacy_event("NodeRenamedEvent", path=moved_path, source_path=initial_path, etag="v2")
    )

    assert created == {"matched": 1, "changed": 1}
    assert duplicate["changed"] == 0
    assert updated["changed"] == 1
    assert moved["changed"] == 1

    async with SessionFactory() as session:
        file = await session.scalar(select(FileRecord))
        assert file is not None
        assert file.watch_id == watch_id
        assert file.relative_path == "525/Математика/8/result.xlsx"
        assert file.parent_path == "525/Математика/8"
        assert file.extension == "xlsx"
        assert file.revision_count == 1
        assert file.nextcloud_mtime.year == 2020
        assert file.nextcloud_upload_time.replace(tzinfo=UTC) == datetime(2026, 9, 10, 10, 0, tzinfo=UTC)
        activities = list((await session.scalars(select(ActivityEvent).order_by(ActivityEvent.id))).all())
        assert [item.event_type.value for item in activities] == ["created", "updated", "moved"]

        report, _ = await build_xlsx(session, watch_id)
        assert report.stat().st_size > 0
        remove_report(report)
        changes, _ = await build_changes_xlsx(
            session,
            watch_id,
            datetime(2020, 1, 1, tzinfo=UTC),
            datetime(2030, 1, 1, tzinfo=UTC),
        )
        workbook = __import__("openpyxl").load_workbook(changes, read_only=True, data_only=True)
        assert workbook.sheetnames == ["Итоги", "Изменения"]
        categories = [row[1] for row in workbook["Изменения"].iter_rows(min_row=2, values_only=True)]
        assert categories == ["Добавлено", "Изменено", "Перемещено"]
        workbook.close()
        remove_report(changes)

    deleted = await process_event_payload(legacy_event("NodeDeletedEvent", path=moved_path, etag="v2"))
    assert deleted["changed"] == 1
    async with SessionFactory() as session:
        file = await session.scalar(select(FileRecord))
        assert file.exists is False
        assert await session.scalar(select(func.count(ActivityEvent.id))) == 4


@pytest.mark.asyncio
async def test_folder_creation_is_visible_activity() -> None:
    await seed_watch()
    created = await process_event_payload(
        legacy_event("NodeCreatedEvent", path="/Олимпиада 2026/525/Математика", file_type="dir")
    )
    assert created == {"matched": 1, "changed": 1}
    async with SessionFactory() as session:
        activity = await session.scalar(select(ActivityEvent))
        assert activity is not None
        assert activity.node_type == "folder"
        assert activity.path == "/Олимпиада 2026/525/Математика"
        assert await session.scalar(select(func.count(FileRecord.id))) == 0
        folder = await session.scalar(select(FolderRecord))
        assert folder is not None
        assert folder.relative_path == "525/Математика"


@pytest.mark.asyncio
async def test_reconciliation_recovers_empty_folder_create_and_delete(monkeypatch) -> None:
    import nc_py_api

    watch_id = await seed_watch()
    current = [
        NodeSnapshot(
            owner_user_id="collector",
            path="/Олимпиада 2026/Пустая папка",
            file_id="folder-1",
            is_dir=True,
        )
    ]

    async def fake_tree(_nc, _owner, _root):
        return list(current)

    monkeypatch.setattr(reconciliation_service, "_tree_snapshots", fake_tree)
    monkeypatch.setattr(nc_py_api, "AsyncNextcloudApp", lambda **_kwargs: object())
    created = await reconciliation_service.reconcile_watch(watch_id)
    assert created["created"] == 1

    current.clear()
    deleted = await reconciliation_service.reconcile_watch(watch_id)
    assert deleted["missing"] == 1
    async with SessionFactory() as session:
        folder = await session.scalar(select(FolderRecord))
        assert folder is not None
        assert folder.exists is False
        activities = list((await session.scalars(select(ActivityEvent).order_by(ActivityEvent.id))).all())
        assert [item.event_type.value for item in activities] == ["created", "deleted"]


@pytest.mark.asyncio
async def test_reconciliation_respects_watch_period() -> None:
    watch_id = await seed_watch()
    async with SessionFactory() as session:
        watch = await session.get(Watch, watch_id)
        watch.started_at = datetime(2030, 1, 1, tzinfo=UTC)
        await session.commit()

    result = await reconciliation_service.reconcile_watch(watch_id)
    assert result["status"] == "skipped"
    async with SessionFactory() as session:
        assert await session.scalar(select(func.count(reconciliation_service.ReconciliationRun.id))) == 0


@pytest.mark.asyncio
async def test_reconciliation_and_delayed_webhook_do_not_duplicate_creation() -> None:
    watch_id = await seed_watch()
    path = "/Олимпиада 2026/525/result.xlsx"
    async with SessionFactory() as session:
        file = FileRecord(
            watch_id=watch_id,
            nextcloud_file_id="9001",
            filename="result.xlsx",
            full_path=path,
            relative_path="525/result.xlsx",
            parent_path="525",
            mime_type="application/test",
            etag="v1",
            first_seen_at=datetime(2026, 9, 10, 10, 0, tzinfo=UTC),
            last_seen_at=datetime(2026, 9, 10, 10, 0, tzinfo=UTC),
        )
        session.add(file)
        await session.flush()
        await reconciliation_event(
            session,
            file,
            EventType.CREATED,
            path,
            "system",
            event_time=datetime(2026, 9, 10, 10, 0, tzinfo=UTC),
        )
        await session.commit()

    delayed = await process_event_payload(legacy_event("NodeCreatedEvent", path=path))

    assert delayed == {"matched": 1, "changed": 0}
    async with SessionFactory() as session:
        assert await session.scalar(select(func.count(ActivityEvent.id))) == 1


def test_reconciliation_refreshes_metadata_without_content_change() -> None:
    old_time = datetime(2026, 9, 10, 10, 0)
    file = FileRecord(
        watch_id=1,
        nextcloud_file_id="9001",
        filename="result.xlsx",
        full_path="/Олимпиада 2026/result.xlsx",
        relative_path="result.xlsx",
        parent_path="",
        mime_type="application/octet-stream",
        extension="xlsx",
        size=1024,
        etag="v1",
        first_seen_at=old_time,
        last_seen_at=old_time,
        exists=True,
    )
    snapshot = NodeSnapshot(
        owner_user_id="collector",
        path=file.full_path,
        file_id="9001",
        etag="v1",
        mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size=1024,
        mtime=datetime(2020, 1, 1, tzinfo=UTC),
        upload_time=datetime(2026, 9, 10, 10, 0, tzinfo=UTC),
    )

    changes = refresh_file_metadata(file, snapshot, "result.xlsx", "", "xlsx")

    assert changes == (False, False, False, True)
    assert file.nextcloud_upload_time == snapshot.upload_time
    assert file.nextcloud_mtime == snapshot.mtime
    assert file.mime_type == snapshot.mime_type


@pytest.mark.asyncio
async def test_team_watch_accepts_events_from_selected_group(monkeypatch) -> None:
    async def selected_groups(_user_id: str) -> set[str]:
        return {"schools"}

    monkeypatch.setattr(event_service, "_event_user_groups", selected_groups)
    await seed_watch(owner_user_id="admin", scope_type="team", group_ids=["schools"])
    result = await process_event_payload(legacy_event("NodeCreatedEvent", path="/Олимпиада 2026/525/result.xlsx"))
    assert result == {"matched": 1, "changed": 1}


@pytest.mark.asyncio
async def test_outside_tree_is_ignored() -> None:
    await seed_watch()
    result = await process_event_payload(legacy_event("NodeCreatedEvent", path="/Другая папка/любая/глубина/file.xlsx"))
    assert result == {"matched": 0, "changed": 0}
