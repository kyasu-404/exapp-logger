from __future__ import annotations

import math

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models import Organization
from ..schemas import OrganizationCreate, OrganizationUpdate
from ..security import require_admin, require_observer
from ..serialization import organization_dict

router = APIRouter(prefix="/api/organizations", tags=["organizations"])


@router.get("")
async def list_organizations(
    search: str = "",
    active: bool | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    _user: str = Depends(require_observer),
    session: AsyncSession = Depends(get_session),
):
    filters = []
    if search:
        pattern = f"%{search.strip()}%"
        filters.append(
            or_(
                Organization.code.ilike(pattern),
                Organization.name.ilike(pattern),
                Organization.short_name.ilike(pattern),
                Organization.folder_name.ilike(pattern),
            )
        )
    if active is not None:
        filters.append(Organization.active.is_(active))
    total = await session.scalar(select(func.count(Organization.id)).where(*filters)) or 0
    items = list(
        (
            await session.scalars(
                select(Organization)
                .where(*filters)
                .order_by(Organization.code)
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).all()
    )
    return {
        "items": [organization_dict(item) for item in items],
        "page": page,
        "page_size": page_size,
        "total": total,
        "pages": math.ceil(total / page_size) if total else 0,
    }


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_organization(
    payload: OrganizationCreate,
    _user: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    item = Organization(**payload.model_dump())
    session.add(item)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Код или имя папки уже используются") from exc
    await session.refresh(item)
    return organization_dict(item)


@router.patch("/{organization_id}")
async def update_organization(
    organization_id: int,
    payload: OrganizationUpdate,
    _user: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    item = await session.get(Organization, organization_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Организация не найдена")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(item, key, value)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Код или имя папки уже используются") from exc
    return organization_dict(item)


@router.delete("/{organization_id}", status_code=status.HTTP_204_NO_CONTENT)
async def deactivate_organization(
    organization_id: int,
    _user: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    item = await session.get(Organization, organization_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Организация не найдена")
    item.active = False
    await session.commit()
