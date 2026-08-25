from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, selectinload

from ..db import get_session
from ..models import (
    Campaign,
    CampaignOrganization,
    CampaignStatus,
    Dimension,
    DimensionValue,
    EventType,
    FileDimensionValue,
    FileEvent,
    FileRecord,
    Organization,
    ParseStatus,
    Requirement,
)
from ..schemas import CampaignClone, CampaignCreate, CampaignUpdate, RequirementInput
from ..security import require_admin, require_observer
from ..serialization import campaign_dict, event_dict, file_dict
from ..services.classifier import apply_classification, classify
from ..services.imports import requirement_identity
from ..services.progress import build_matrix, build_progress, build_summary, filter_progress
from ..services.reconciliation import reconcile_campaign

router = APIRouter(prefix="/api/campaigns", tags=["campaigns"])


def utcnow() -> datetime:
    return datetime.now(UTC)


def campaign_options():
    return (
        selectinload(Campaign.organizations).selectinload(CampaignOrganization.organization),
        selectinload(Campaign.dimensions).selectinload(Dimension.values),
        selectinload(Campaign.requirements).selectinload(Requirement.organization),
    )


async def load_campaign(session: AsyncSession, campaign_id: int) -> Campaign:
    item = await session.scalar(select(Campaign).where(Campaign.id == campaign_id).options(*campaign_options()))
    if item is None:
        raise HTTPException(status_code=404, detail="Кампания не найдена")
    return item


async def _validate_organizations(session: AsyncSession, ids: list[int]) -> list[Organization]:
    if not ids:
        return []
    items = list((await session.scalars(select(Organization).where(Organization.id.in_(set(ids))))).all())
    if len(items) != len(set(ids)):
        raise HTTPException(status_code=422, detail="Некоторые организации не найдены")
    return items


def _add_dimensions(campaign: Campaign, payloads) -> None:
    for position, payload in enumerate(payloads, start=1):
        dimension = Dimension(
            position=position,
            key=payload.key,
            name=payload.name,
            required=payload.required,
            fixed=payload.fixed,
        )
        for value in payload.values:
            dimension.values.append(
                DimensionValue(
                    value=value.value.strip(),
                    display_name=(value.display_name or value.value).strip(),
                    sort_order=value.sort_order,
                    active=value.active,
                )
            )
        campaign.dimensions.append(dimension)


@router.get("")
async def list_campaigns(
    campaign_status: CampaignStatus | None = Query(default=None, alias="status"),
    year: int | None = None,
    search: str = "",
    _user: str = Depends(require_observer),
    session: AsyncSession = Depends(get_session),
):
    filters = []
    if campaign_status:
        filters.append(Campaign.status == campaign_status)
    if year:
        filters.append(Campaign.year == year)
    if search:
        filters.append(Campaign.name.ilike(f"%{search.strip()}%"))
    items = list(
        (
            await session.scalars(
                select(Campaign).where(*filters).order_by(Campaign.year.desc(), Campaign.created_at.desc())
            )
        ).all()
    )
    return [campaign_dict(item) for item in items]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_campaign(
    payload: CampaignCreate,
    admin_user: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    organizations = await _validate_organizations(session, payload.organization_ids)
    data = payload.model_dump(exclude={"organization_ids", "dimensions"})
    if not data.get("storage_user_id"):
        data["storage_user_id"] = admin_user
    campaign = Campaign(**data, created_by=admin_user)
    for organization in organizations:
        campaign.organizations.append(CampaignOrganization(organization=organization, included=True))
    _add_dimensions(campaign, payload.dimensions)
    session.add(campaign)
    await session.commit()
    return campaign_dict(await load_campaign(session, campaign.id), include_config=True)


@router.get("/{campaign_id}")
async def get_campaign(
    campaign_id: int,
    _user: str = Depends(require_observer),
    session: AsyncSession = Depends(get_session),
):
    return campaign_dict(await load_campaign(session, campaign_id), include_config=True)


async def _reclassify(session: AsyncSession, campaign: Campaign, actor: str) -> int:
    files = list((await session.scalars(select(FileRecord).where(FileRecord.campaign_id == campaign.id))).all())
    changed = 0
    for file in files:
        old_status = file.parse_status
        old_org = file.organization_id
        await apply_classification(session, file, classify(campaign, file.full_path))
        await session.flush()
        if file.parse_status != old_status or file.organization_id != old_org:
            raw = f"reclassify:{campaign.id}:{file.id}:{campaign.updated_at.isoformat()}"
            event_key = hashlib.sha256(raw.encode()).hexdigest()
            if not await session.scalar(select(FileEvent.id).where(FileEvent.event_key == event_key)):
                session.add(
                    FileEvent(
                        file_id=file.id,
                        event_key=event_key,
                        event_type=EventType.RECLASSIFIED,
                        occurred_at=utcnow(),
                        new_path=file.full_path,
                        etag=file.etag,
                        size=file.size,
                        actor_user_id=actor,
                        raw_event={"source": "manual_reclassification"},
                    )
                )
            changed += 1
    return changed


@router.patch("/{campaign_id}")
async def update_campaign(
    campaign_id: int,
    payload: CampaignUpdate,
    admin_user: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    campaign = await load_campaign(session, campaign_id)
    structure_change = (
        payload.dimensions is not None or payload.structure_mode is not None or payload.root_path is not None
    )
    if structure_change and campaign.status == CampaignStatus.ACTIVE and not payload.confirm_reclassification:
        raise HTTPException(
            status_code=409,
            detail="Изменение структуры активной кампании требует подтверждения и повторной классификации файлов",
        )
    simple_fields = payload.model_dump(
        exclude={"organization_ids", "dimensions", "confirm_reclassification"}, exclude_unset=True
    )
    for key, value in simple_fields.items():
        setattr(campaign, key, value)
    if payload.organization_ids is not None:
        organizations = await _validate_organizations(session, payload.organization_ids)
        campaign.organizations.clear()
        for organization in organizations:
            campaign.organizations.append(CampaignOrganization(organization=organization, included=True))
    if payload.dimensions is not None:
        campaign.dimensions.clear()
        await session.flush()
        _add_dimensions(campaign, payload.dimensions)
    campaign.updated_at = utcnow()
    await session.flush()
    changed = await _reclassify(session, campaign, admin_user) if structure_change else 0
    await session.commit()
    result = campaign_dict(await load_campaign(session, campaign_id), include_config=True)
    result["reclassified_files"] = changed
    return result


@router.post("/{campaign_id}/clone", status_code=status.HTTP_201_CREATED)
async def clone_campaign(
    campaign_id: int,
    payload: CampaignClone,
    admin_user: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    source = await load_campaign(session, campaign_id)
    clone = Campaign(
        name=payload.name,
        campaign_type=source.campaign_type,
        year=payload.year,
        description=source.description,
        root_path="/" + payload.root_path.replace("\\", "/").strip("/"),
        storage_user_id=source.storage_user_id,
        start_at=payload.start_at,
        deadline_at=payload.deadline_at,
        status=CampaignStatus.DRAFT,
        structure_mode=source.structure_mode,
        requirements_mode=source.requirements_mode,
        settings=dict(source.settings or {}),
        created_by=admin_user,
    )
    for link in source.organizations:
        if link.included:
            clone.organizations.append(CampaignOrganization(organization_id=link.organization_id, included=True))
    for dimension in source.dimensions:
        copied = Dimension(
            position=dimension.position,
            key=dimension.key,
            name=dimension.name,
            required=dimension.required,
            fixed=dimension.fixed,
        )
        for value in dimension.values:
            copied.values.append(
                DimensionValue(
                    value=value.value,
                    display_name=value.display_name,
                    sort_order=value.sort_order,
                    active=value.active,
                )
            )
        clone.dimensions.append(copied)
    if payload.copy_requirements:
        for requirement in source.requirements:
            clone.requirements.append(
                Requirement(
                    organization_id=requirement.organization_id,
                    identity_key=requirement.identity_key,
                    dimensions=dict(requirement.dimensions or {}),
                    required=requirement.required,
                    required_file_count=requirement.required_file_count,
                    deadline_at=payload.deadline_at
                    if requirement.deadline_at == source.deadline_at
                    else requirement.deadline_at,
                )
            )
    session.add(clone)
    await session.commit()
    return campaign_dict(await load_campaign(session, clone.id), include_config=True)


@router.post("/{campaign_id}/reclassify")
async def reclassify_files(
    campaign_id: int,
    admin_user: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    campaign = await load_campaign(session, campaign_id)
    changed = await _reclassify(session, campaign, admin_user)
    await session.commit()
    return {
        "status": "completed",
        "checked": len(campaign.files) if "files" in campaign.__dict__ else changed,
        "changed": changed,
    }


@router.get("/{campaign_id}/summary")
async def campaign_summary(
    campaign_id: int,
    _user: str = Depends(require_observer),
    session: AsyncSession = Depends(get_session),
):
    result = await build_summary(session, campaign_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Кампания не найдена")
    return result


@router.get("/{campaign_id}/progress")
async def campaign_progress(
    campaign_id: int,
    status_filter: str | None = Query(default=None, alias="status"),
    organization_id: int | None = None,
    search: str = "",
    dimensions: str = "",
    _user: str = Depends(require_observer),
    session: AsyncSession = Depends(get_session),
):
    result = await build_progress(session, campaign_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Кампания не найдена")
    try:
        dimension_filters = json.loads(dimensions) if dimensions else {}
        if not isinstance(dimension_filters, dict):
            raise ValueError
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Некорректный фильтр уровней") from exc
    return filter_progress(
        result,
        status_filter=status_filter or "",
        organization_id=organization_id,
        search=search,
        dimensions=dimension_filters,
    )


@router.get("/{campaign_id}/matrix")
async def campaign_matrix(
    campaign_id: int,
    column_dimension: str,
    filter_dimension: str | None = None,
    filter_value: str | None = None,
    _user: str = Depends(require_observer),
    session: AsyncSession = Depends(get_session),
):
    result = await build_matrix(session, campaign_id, column_dimension, filter_dimension, filter_value)
    if result is None:
        raise HTTPException(status_code=404, detail="Кампания не найдена")
    return result


@router.get("/{campaign_id}/files")
async def campaign_files(
    campaign_id: int,
    organization_id: int | None = None,
    parse_status: ParseStatus | None = None,
    deleted: bool | None = None,
    updated_only: bool = False,
    overdue_only: bool = False,
    attention_only: bool = False,
    extension: str = "",
    search: str = "",
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    dimension_key: str | None = None,
    dimension_value: str | None = None,
    sort: str = "first_received_at",
    direction: str = "desc",
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    _user: str = Depends(require_observer),
    session: AsyncSession = Depends(get_session),
):
    campaign = await load_campaign(session, campaign_id)
    filters = [FileRecord.campaign_id == campaign_id]
    if organization_id:
        filters.append(FileRecord.organization_id == organization_id)
    if parse_status:
        filters.append(FileRecord.parse_status == parse_status)
    if deleted is not None:
        filters.append(FileRecord.deleted.is_(deleted))
    if updated_only:
        filters.append(FileRecord.revision_count > 0)
    if overdue_only and campaign.deadline_at:
        filters.append(FileRecord.first_received_at > campaign.deadline_at)
    if attention_only:
        filters.extend([FileRecord.parse_status != ParseStatus.OK, FileRecord.warning_ignored.is_(False)])
    if date_from:
        filters.append(FileRecord.first_received_at >= date_from)
    if date_to:
        filters.append(FileRecord.first_received_at <= date_to)
    if extension:
        filters.append(FileRecord.filename.ilike(f"%.{extension.lstrip('.')}"))
    if search:
        pattern = f"%{search.strip()}%"
        filters.append(
            or_(
                FileRecord.filename.ilike(pattern),
                FileRecord.full_path.ilike(pattern),
                FileRecord.organization.has(
                    or_(
                        Organization.name.ilike(pattern),
                        Organization.short_name.ilike(pattern),
                        Organization.code.ilike(pattern),
                    )
                ),
            )
        )
    if dimension_key and dimension_value:
        dim = aliased(Dimension)
        link = aliased(FileDimensionValue)
        filters.append(
            exists(
                select(1)
                .select_from(link)
                .join(dim, link.dimension_id == dim.id)
                .where(
                    link.file_id == FileRecord.id,
                    dim.key == dimension_key,
                    link.raw_value == dimension_value,
                )
            )
        )
    total = await session.scalar(select(func.count(FileRecord.id)).where(*filters)) or 0
    sort_columns = {
        "filename": FileRecord.filename,
        "size": FileRecord.size,
        "first_received_at": FileRecord.first_received_at,
        "last_received_at": FileRecord.last_received_at,
        "revision_count": FileRecord.revision_count,
    }
    sort_column = sort_columns.get(sort, FileRecord.first_received_at)
    order = sort_column.asc() if direction == "asc" else sort_column.desc()
    files = list(
        (
            await session.scalars(
                select(FileRecord)
                .where(*filters)
                .options(
                    selectinload(FileRecord.organization),
                    selectinload(FileRecord.dimension_values).selectinload(FileDimensionValue.dimension),
                )
                .order_by(order, FileRecord.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).all()
    )
    return {
        "items": [file_dict(file) for file in files],
        "page": page,
        "page_size": page_size,
        "total": total,
        "pages": math.ceil(total / page_size) if total else 0,
    }


@router.patch("/{campaign_id}/files/{file_id}/ignore")
async def ignore_warning(
    campaign_id: int,
    file_id: int,
    ignored: bool = True,
    _user: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    file = await session.scalar(
        select(FileRecord).where(FileRecord.id == file_id, FileRecord.campaign_id == campaign_id)
    )
    if file is None:
        raise HTTPException(status_code=404, detail="Файл не найден")
    file.warning_ignored = ignored
    if ignored:
        file.parse_status = ParseStatus.IGNORED
    await session.commit()
    return {"status": "ok"}


@router.get("/{campaign_id}/events")
async def campaign_events(
    campaign_id: int,
    organization_id: int | None = None,
    event_type: EventType | None = None,
    search: str = "",
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    dimension_key: str | None = None,
    dimension_value: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    _user: str = Depends(require_observer),
    session: AsyncSession = Depends(get_session),
):
    filters = [FileRecord.campaign_id == campaign_id]
    if organization_id:
        filters.append(FileRecord.organization_id == organization_id)
    if event_type:
        filters.append(FileEvent.event_type == event_type)
    if search:
        pattern = f"%{search.strip()}%"
        filters.append(or_(FileRecord.filename.ilike(pattern), FileEvent.new_path.ilike(pattern)))
    if date_from:
        filters.append(FileEvent.occurred_at >= date_from)
    if date_to:
        filters.append(FileEvent.occurred_at <= date_to)
    if dimension_key and dimension_value:
        dim = aliased(Dimension)
        link = aliased(FileDimensionValue)
        filters.append(
            exists(
                select(1)
                .select_from(link)
                .join(dim, link.dimension_id == dim.id)
                .where(
                    link.file_id == FileRecord.id,
                    dim.key == dimension_key,
                    link.raw_value == dimension_value,
                )
            )
        )
    base = select(FileEvent).join(FileEvent.file).where(*filters)
    total = await session.scalar(select(func.count()).select_from(base.subquery())) or 0
    events = list(
        (
            await session.scalars(
                base.options(
                    selectinload(FileEvent.file).selectinload(FileRecord.organization),
                    selectinload(FileEvent.file)
                    .selectinload(FileRecord.dimension_values)
                    .selectinload(FileDimensionValue.dimension),
                )
                .order_by(FileEvent.occurred_at.desc(), FileEvent.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).all()
    )
    return {
        "items": [event_dict(event) for event in events],
        "page": page,
        "page_size": page_size,
        "total": total,
        "pages": math.ceil(total / page_size) if total else 0,
    }


@router.get("/{campaign_id}/requirements")
async def campaign_requirements(
    campaign_id: int,
    _user: str = Depends(require_observer),
    session: AsyncSession = Depends(get_session),
):
    progress = await build_progress(session, campaign_id)
    if progress is None:
        raise HTTPException(status_code=404, detail="Кампания не найдена")
    details = []
    for organization in progress["organizations"]:
        for requirement in organization["requirements"]:
            details.append({**requirement, "organization": organization["organization"]})
    return details


@router.put("/{campaign_id}/requirements")
async def replace_requirements(
    campaign_id: int,
    payload: list[RequirementInput],
    _user: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    campaign = await load_campaign(session, campaign_id)
    participant_ids = {link.organization_id for link in campaign.organizations if link.included}
    if any(item.organization_id not in participant_ids for item in payload):
        raise HTTPException(status_code=422, detail="Требование содержит организацию вне кампании")
    await session.execute(delete(Requirement).where(Requirement.campaign_id == campaign_id))
    for item in payload:
        data = item.model_dump()
        session.add(
            Requirement(
                campaign_id=campaign_id,
                identity_key=requirement_identity(data["dimensions"]),
                **data,
            )
        )
    campaign.requirements_mode = "matrix" if payload else "simple"
    await session.commit()
    return {"status": "ok", "count": len(payload)}


@router.post("/{campaign_id}/reconcile")
async def manual_reconcile(campaign_id: int, admin_user: str = Depends(require_admin)):
    try:
        return await reconcile_campaign(campaign_id, admin_user)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Сверка с Nextcloud не выполнена: {exc}") from exc
