from __future__ import annotations

import asyncio
import calendar
import logging
from collections import defaultdict
from contextlib import suppress
from datetime import UTC, date, datetime, time, timedelta
from pathlib import PurePosixPath

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from ..db import SessionFactory
from ..models import ReportCadence, ReportSchedule, Watch, WatchStatus
from ..time_utils import as_utc, local_datetime_utc, local_timezone
from .events import relative_to_watch
from .reporting import build_xlsx, remove_report

LOGGER = logging.getLogger(__name__)
_LOCKS: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


def utcnow() -> datetime:
    return datetime.now(UTC)


def _clock(value: str) -> time:
    hour, minute = (int(part) for part in value.split(":", 1))
    return time(hour, minute)


def calculate_next_run(schedule: ReportSchedule, after: datetime | None = None) -> datetime | None:
    if not schedule.enabled:
        return None
    moment = (as_utc(after) or utcnow()).astimezone(local_timezone())
    clock = _clock(schedule.local_time)

    if schedule.cadence == ReportCadence.DAILY:
        candidate_date = moment.date()
        candidate = local_datetime_utc(candidate_date, clock)
        if candidate <= moment.astimezone(UTC):
            candidate = local_datetime_utc(candidate_date + timedelta(days=1), clock)
        return candidate

    if schedule.cadence == ReportCadence.WEEKLY:
        days = (schedule.weekday - moment.weekday()) % 7
        candidate_date = moment.date() + timedelta(days=days)
        candidate = local_datetime_utc(candidate_date, clock)
        if candidate <= moment.astimezone(UTC):
            candidate = local_datetime_utc(candidate_date + timedelta(days=7), clock)
        return candidate

    year, month = moment.year, moment.month
    for _ in range(14):
        day = min(schedule.day_of_month, calendar.monthrange(year, month)[1])
        candidate = local_datetime_utc(date(year, month, day), clock)
        if candidate > moment.astimezone(UTC):
            return candidate
        month += 1
        if month > 12:
            year += 1
            month = 1
    raise RuntimeError("Не удалось рассчитать следующий запуск отчёта")


async def validate_destination(session, watch: Watch, destination_path: str) -> None:
    if not destination_path:
        raise ValueError("Выберите папку Nextcloud для автоматических отчётов")
    watched = list(
        (
            await session.scalars(
                select(Watch).where(
                    Watch.owner_user_id == watch.owner_user_id,
                    Watch.status != WatchStatus.ARCHIVED,
                )
            )
        ).all()
    )
    for item in watched:
        if relative_to_watch(destination_path, item.root_path) is not None:
            raise ValueError(
                f"Папка отчётов находится внутри наблюдения «{item.name}»; это вызовет циклические события"
            )


async def validate_remote_folder(watch: Watch, destination_path: str) -> None:
    from nc_py_api import AsyncNextcloudApp

    node = await AsyncNextcloudApp(user=watch.owner_user_id).files.by_path(destination_path.lstrip("/"))
    if node is None or not node.is_dir:
        raise ValueError("Выбранная папка Nextcloud не найдена")
    if not node.is_creatable:
        raise ValueError("В выбранную папку Nextcloud нельзя записывать файлы")


async def run_scheduled_report(schedule_id: int, *, manual: bool = False) -> dict[str, str | int]:
    async with _LOCKS[schedule_id]:
        async with SessionFactory() as session:
            schedule = await session.scalar(
                select(ReportSchedule)
                .where(ReportSchedule.id == schedule_id)
                .options(selectinload(ReportSchedule.watch))
            )
            if schedule is None:
                raise ValueError("Расписание не найдено")
            watch = schedule.watch
            if not schedule.destination_path:
                raise ValueError("Не выбрана папка для отчётов")
            schedule.last_started_at = utcnow()
            schedule.last_status = "running"
            schedule.last_error = ""
            await session.commit()
            watch_id = watch.id
            owner = watch.owner_user_id
            destination = schedule.destination_path

        report_path = None
        try:
            async with SessionFactory() as session:
                report_path, filename = await build_xlsx(session, watch_id)
            from nc_py_api import AsyncNextcloudApp

            remote_path = str(PurePosixPath(destination) / filename)
            await AsyncNextcloudApp(user=owner).files.upload_stream(remote_path.lstrip("/"), report_path)
            finished = utcnow()
            async with SessionFactory() as session:
                schedule = await session.get(ReportSchedule, schedule_id)
                if schedule:
                    schedule.last_finished_at = finished
                    schedule.last_status = "completed"
                    schedule.last_error = ""
                    schedule.last_file_path = remote_path
                    schedule.next_run_at = calculate_next_run(schedule, finished)
                    await session.commit()
            LOGGER.info("Автоматический отчёт %s сохранён в %s", schedule_id, remote_path)
            return {"status": "completed", "path": remote_path, "schedule_id": schedule_id}
        except Exception as exc:
            LOGGER.exception("Ошибка автоматического отчёта %s", schedule_id)
            async with SessionFactory() as session:
                schedule = await session.get(ReportSchedule, schedule_id)
                if schedule:
                    finished = utcnow()
                    schedule.last_finished_at = finished
                    schedule.last_status = "failed"
                    schedule.last_error = str(exc)[:4000]
                    schedule.next_run_at = calculate_next_run(schedule, finished)
                    await session.commit()
            raise
        finally:
            if report_path is not None:
                remove_report(report_path)


async def run_due_reports() -> None:
    now = utcnow()
    async with SessionFactory() as session:
        ids = list(
            (
                await session.scalars(
                    select(ReportSchedule.id).where(
                        ReportSchedule.enabled.is_(True),
                        ReportSchedule.next_run_at.is_not(None),
                        ReportSchedule.next_run_at <= now,
                    )
                )
            ).all()
        )

    semaphore = asyncio.Semaphore(2)

    async def run_one(schedule_id: int) -> None:
        async with semaphore:
            with suppress(Exception):
                await run_scheduled_report(schedule_id)

    await asyncio.gather(*(run_one(schedule_id) for schedule_id in ids))
