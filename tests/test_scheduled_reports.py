from __future__ import annotations

from datetime import UTC, datetime

import pytest

from file_control.db import SessionFactory
from file_control.models import ReportCadence, ReportSchedule, Watch, WatchStatus
from file_control.services.scheduled_reports import (
    calculate_next_run,
    run_scheduled_report,
    validate_destination,
)


def schedule(cadence: ReportCadence, **values) -> ReportSchedule:
    return ReportSchedule(
        watch_id=1,
        enabled=True,
        cadence=cadence,
        local_time=values.get("local_time", "09:00"),
        weekday=values.get("weekday", 0),
        day_of_month=values.get("day_of_month", 1),
        destination_path="/Отчёты",
    )


def test_next_run_uses_moscow_time() -> None:
    daily = calculate_next_run(
        schedule(ReportCadence.DAILY, local_time="09:00"),
        datetime(2026, 8, 27, 7, 0, tzinfo=UTC),
    )
    assert daily == datetime(2026, 8, 28, 6, 0, tzinfo=UTC)

    weekly = calculate_next_run(
        schedule(ReportCadence.WEEKLY, local_time="10:30", weekday=4),
        datetime(2026, 8, 27, 7, 0, tzinfo=UTC),
    )
    assert weekly == datetime(2026, 8, 28, 7, 30, tzinfo=UTC)

    monthly = calculate_next_run(
        schedule(ReportCadence.MONTHLY, local_time="08:15", day_of_month=1),
        datetime(2026, 8, 27, 7, 0, tzinfo=UTC),
    )
    assert monthly == datetime(2026, 9, 1, 5, 15, tzinfo=UTC)


@pytest.mark.asyncio
async def test_report_destination_cannot_be_inside_any_watch() -> None:
    async with SessionFactory() as session:
        watch = Watch(
            name="Олимпиада",
            root_path="/Олимпиада",
            owner_user_id="admin",
            created_by="admin",
            status=WatchStatus.ACTIVE,
        )
        session.add(watch)
        await session.commit()
        await session.refresh(watch)

        with pytest.raises(ValueError, match="циклические события"):
            await validate_destination(session, watch, "/Олимпиада/Отчёты")
        await validate_destination(session, watch, "/Автоматические отчёты")


@pytest.mark.asyncio
async def test_scheduled_report_is_uploaded_and_status_is_saved(monkeypatch) -> None:
    import nc_py_api

    uploaded: dict[str, str] = {}

    class FakeFiles:
        async def upload_stream(self, path, local_path):
            assert local_path.exists()
            assert local_path.stat().st_size > 0
            uploaded["path"] = path

    class FakeNextcloud:
        def __init__(self, **_kwargs):
            self.files = FakeFiles()

    monkeypatch.setattr(nc_py_api, "AsyncNextcloudApp", FakeNextcloud)

    async with SessionFactory() as session:
        watch = Watch(
            name="Олимпиада",
            root_path="/Олимпиада",
            owner_user_id="admin",
            created_by="admin",
            status=WatchStatus.ACTIVE,
        )
        session.add(watch)
        await session.flush()
        report_schedule = ReportSchedule(
            watch_id=watch.id,
            enabled=True,
            cadence=ReportCadence.DAILY,
            local_time="09:00",
            destination_path="/Автоматические отчёты",
        )
        session.add(report_schedule)
        await session.commit()
        schedule_id = report_schedule.id

    result = await run_scheduled_report(schedule_id)
    assert result["status"] == "completed"
    assert uploaded["path"].startswith("Автоматические отчёты/Наблюдение_Олимпиада_")

    async with SessionFactory() as session:
        saved = await session.get(ReportSchedule, schedule_id)
        assert saved.last_status == "completed"
        assert saved.last_error == ""
        assert saved.last_file_path.startswith("/Автоматические отчёты/")
        assert saved.next_run_at is not None
