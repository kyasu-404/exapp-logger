from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import in_nextcloud_runtime
from ..db import get_session
from ..models import EventType, ReportCadence, ReportSchedule, Watch
from ..schemas import ReportScheduleUpdate
from ..security import ensure_watch_access, require_admin, require_observer
from ..serialization import report_schedule_dict
from ..services.reporting import build_changes_xlsx, build_csv, build_xlsx, remove_report
from ..services.scheduled_reports import (
    calculate_next_run,
    run_scheduled_report,
    validate_destination,
    validate_remote_folder,
)

router = APIRouter(prefix="/api/reports", tags=["reports"])
REPORT_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
    "X-Content-Type-Options": "nosniff",
}


async def ensure_report_access(session: AsyncSession, watch_id: int, user_id: str) -> None:
    watch = await session.get(Watch, watch_id)
    if watch is None:
        raise HTTPException(status_code=404, detail="Наблюдение не найдено")
    await ensure_watch_access(user_id, watch)


async def report_watch(session: AsyncSession, watch_id: int, user_id: str) -> Watch:
    watch = await session.get(Watch, watch_id)
    if watch is None:
        raise HTTPException(status_code=404, detail="Наблюдение не найдено")
    await ensure_watch_access(user_id, watch)
    return watch


def filters(
    *,
    search: str,
    extension: str,
    status: str,
    event_type: EventType | None,
    path_prefix: str,
    date_from: datetime | None,
    date_to: datetime | None,
) -> dict:
    return {
        "search": search,
        "extension": extension,
        "status": status,
        "event_type": event_type,
        "path_prefix": path_prefix,
        "date_from": date_from,
        "date_to": date_to,
    }


@router.get("/xlsx")
async def xlsx_report(
    watch_id: int,
    background_tasks: BackgroundTasks,
    search: str = "",
    extension: str = "",
    status_filter: str = Query(default="", alias="status", pattern="^(|new|updated|deleted)$"),
    event_type: EventType | None = None,
    path_prefix: str = "",
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    current_user: str = Depends(require_observer),
    session: AsyncSession = Depends(get_session),
):
    await ensure_report_access(session, watch_id, current_user)
    try:
        path, filename = await build_xlsx(
            session,
            watch_id,
            filters(
                search=search,
                extension=extension,
                status=status_filter,
                event_type=event_type,
                path_prefix=path_prefix,
                date_from=date_from,
                date_to=date_to,
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    background_tasks.add_task(remove_report, path)
    return FileResponse(
        path,
        filename=filename,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=REPORT_HEADERS,
        background=background_tasks,
    )


@router.get("/changes/xlsx")
async def changes_xlsx_report(
    watch_id: int,
    date_from: datetime,
    date_to: datetime,
    background_tasks: BackgroundTasks,
    path_prefix: str = "",
    current_user: str = Depends(require_observer),
    session: AsyncSession = Depends(get_session),
):
    await ensure_report_access(session, watch_id, current_user)
    try:
        path, filename = await build_changes_xlsx(session, watch_id, date_from, date_to, path_prefix)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    background_tasks.add_task(remove_report, path)
    return FileResponse(
        path,
        filename=filename,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=REPORT_HEADERS,
        background=background_tasks,
    )


@router.get("/csv")
async def csv_report(
    watch_id: int,
    background_tasks: BackgroundTasks,
    table: str = Query(default="files", pattern="^(files|events|folders)$"),
    search: str = "",
    extension: str = "",
    status_filter: str = Query(default="", alias="status", pattern="^(|new|updated|deleted)$"),
    event_type: EventType | None = None,
    path_prefix: str = "",
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    current_user: str = Depends(require_observer),
    session: AsyncSession = Depends(get_session),
):
    await ensure_report_access(session, watch_id, current_user)
    try:
        path, filename = await build_csv(
            session,
            watch_id,
            table,
            filters(
                search=search,
                extension=extension,
                status=status_filter,
                event_type=event_type,
                path_prefix=path_prefix,
                date_from=date_from,
                date_to=date_to,
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    background_tasks.add_task(remove_report, path)
    return FileResponse(
        path,
        filename=filename,
        media_type="text/csv; charset=utf-8",
        headers=REPORT_HEADERS,
        background=background_tasks,
    )


@router.get("/schedule/{watch_id}")
async def get_report_schedule(
    watch_id: int,
    current_user: str = Depends(require_observer),
    session: AsyncSession = Depends(get_session),
):
    await report_watch(session, watch_id, current_user)
    schedule = await session.scalar(select(ReportSchedule).where(ReportSchedule.watch_id == watch_id))
    return report_schedule_dict(schedule)


@router.put("/schedule/{watch_id}")
async def update_report_schedule(
    watch_id: int,
    payload: ReportScheduleUpdate,
    admin_user: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    watch = await report_watch(session, watch_id, admin_user)
    if payload.destination_path:
        try:
            await validate_destination(session, watch, payload.destination_path)
            if in_nextcloud_runtime():
                await validate_remote_folder(watch, payload.destination_path)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Не удалось проверить папку Nextcloud: {exc}") from exc
    schedule = await session.scalar(select(ReportSchedule).where(ReportSchedule.watch_id == watch_id))
    if schedule is None:
        schedule = ReportSchedule(watch_id=watch_id)
        session.add(schedule)
    values = payload.model_dump()
    for field, value in values.items():
        setattr(schedule, field, ReportCadence(value) if field == "cadence" else value)
    schedule.next_run_at = calculate_next_run(schedule)
    await session.commit()
    await session.refresh(schedule)
    return report_schedule_dict(schedule)


@router.post("/schedule/{watch_id}/run")
async def run_report_schedule_now(
    watch_id: int,
    admin_user: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    await report_watch(session, watch_id, admin_user)
    schedule = await session.scalar(select(ReportSchedule).where(ReportSchedule.watch_id == watch_id))
    if schedule is None or not schedule.destination_path:
        raise HTTPException(status_code=409, detail="Сначала сохраните расписание и выберите папку")
    schedule_id = schedule.id
    await session.close()
    try:
        return await run_scheduled_report(schedule_id, manual=True)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Отчёт не сохранён в Nextcloud: {exc}") from exc
