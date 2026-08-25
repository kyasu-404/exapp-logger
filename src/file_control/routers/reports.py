from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models import EventType, ParseStatus
from ..security import require_observer
from ..services.reporting import build_csv, build_xlsx, remove_report

router = APIRouter(prefix="/api/reports", tags=["reports"])


def _dimension_filters(raw: str, dimension_key: str, dimension_value: str) -> dict[str, str]:
    try:
        result = json.loads(raw) if raw else {}
        if not isinstance(result, dict):
            raise ValueError
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Некорректный фильтр уровней") from exc
    if dimension_key and dimension_value:
        result[dimension_key] = dimension_value
    return {str(key): str(value) for key, value in result.items() if value not in (None, "")}


@router.get("/xlsx")
async def xlsx_report(
    campaign_id: int,
    background_tasks: BackgroundTasks,
    status_filter: str = Query(default="", alias="status"),
    search: str = "",
    organization_id: int | None = None,
    dimensions: str = "",
    _user: str = Depends(require_observer),
    session: AsyncSession = Depends(get_session),
):
    try:
        path, filename = await build_xlsx(
            session,
            campaign_id,
            {
                "status": status_filter,
                "search": search,
                "organization_id": organization_id,
                "dimensions": _dimension_filters(dimensions, "", ""),
            },
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    background_tasks.add_task(remove_report, path)
    return FileResponse(
        path,
        filename=filename,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        background=background_tasks,
    )


@router.get("/csv")
async def csv_report(
    campaign_id: int,
    background_tasks: BackgroundTasks,
    table: str = Query(default="files", pattern="^(files|events|progress)$"),
    status_filter: str = Query(default="", alias="status"),
    search: str = "",
    organization_id: int | None = None,
    parse_status: ParseStatus | None = None,
    deleted: bool | None = None,
    updated_only: bool = False,
    attention_only: bool = False,
    extension: str = "",
    event_type: EventType | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    dimensions: str = "",
    dimension_key: str = "",
    dimension_value: str = "",
    _user: str = Depends(require_observer),
    session: AsyncSession = Depends(get_session),
):
    try:
        path, filename = await build_csv(
            session,
            campaign_id,
            table,
            {
                "status": status_filter,
                "search": search,
                "organization_id": organization_id,
                "parse_status": parse_status,
                "deleted": deleted,
                "updated_only": updated_only,
                "attention_only": attention_only,
                "extension": extension,
                "event_type": event_type,
                "date_from": date_from,
                "date_to": date_to,
                "dimensions": _dimension_filters(dimensions, dimension_key, dimension_value),
            },
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    background_tasks.add_task(remove_report, path)
    return FileResponse(path, filename=filename, media_type="text/csv; charset=utf-8", background=background_tasks)
