from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db import get_session
from ..security import require_admin
from ..services.imports import (
    import_dimension_values,
    import_organizations,
    import_requirements,
    read_tabular,
)

router = APIRouter(prefix="/api/import", tags=["imports"])


async def _rows(file: UploadFile) -> list[dict]:
    content = await file.read()
    max_size = get_settings().max_upload_mb * 1024 * 1024
    if len(content) > max_size:
        raise HTTPException(status_code=413, detail="Файл импорта слишком большой")
    try:
        return read_tabular(file.filename or "", content)
    except (ValueError, StopIteration) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/organizations")
async def organizations_import(
    file: UploadFile = File(...),
    commit: bool = Query(default=False),
    _user: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    return await import_organizations(session, await _rows(file), commit)


@router.post("/requirements")
async def requirements_import(
    campaign_id: int,
    file: UploadFile = File(...),
    commit: bool = Query(default=False),
    _user: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    try:
        return await import_requirements(session, campaign_id, await _rows(file), commit)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/dimensions")
async def dimensions_import(
    campaign_id: int,
    file: UploadFile = File(...),
    commit: bool = Query(default=False),
    _user: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    try:
        return await import_dimension_values(session, campaign_id, await _rows(file), commit)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
