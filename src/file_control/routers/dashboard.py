from __future__ import annotations

from datetime import UTC, datetime, time

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings, in_nextcloud_runtime
from ..db import get_session
from ..models import Campaign, CampaignStatus, FileRecord, ParseStatus
from ..security import require_admin, require_observer
from ..serialization import campaign_dict
from ..services.progress import build_summary

router = APIRouter(prefix="/api", tags=["dashboard"])


@router.get("/dashboard")
async def dashboard(
    _user: str = Depends(require_observer),
    session: AsyncSession = Depends(get_session),
):
    campaigns = list(
        (
            await session.scalars(
                select(Campaign).where(Campaign.status == CampaignStatus.ACTIVE).order_by(Campaign.deadline_at)
            )
        ).all()
    )
    summaries = []
    for campaign in campaigns:
        summary = await build_summary(session, campaign.id)
        summaries.append({"campaign": campaign_dict(campaign), "summary": summary})
    start_today = datetime.combine(datetime.now(UTC).date(), time.min, tzinfo=UTC)
    files_today = (
        await session.scalar(select(func.count(FileRecord.id)).where(FileRecord.first_received_at >= start_today)) or 0
    )
    organizations_today = (
        await session.scalar(
            select(func.count(func.distinct(FileRecord.organization_id))).where(
                FileRecord.first_received_at >= start_today,
                FileRecord.organization_id.is_not(None),
            )
        )
        or 0
    )
    unrecognized = (
        await session.scalar(
            select(func.count(FileRecord.id)).where(
                FileRecord.parse_status != ParseStatus.OK,
                FileRecord.warning_ignored.is_(False),
            )
        )
        or 0
    )
    return {
        "metrics": {
            "active_campaigns": len(campaigns),
            "files_today": files_today,
            "organizations_today": organizations_today,
            "unrecognized": unrecognized,
        },
        "campaigns": summaries,
    }


@router.get("/folders")
async def folders(
    path: str = "",
    user_id: str | None = None,
    current_user: str = Depends(require_admin),
):
    if not in_nextcloud_runtime():
        return {"user_id": user_id or current_user, "path": path, "items": []}
    try:
        from nc_py_api import AsyncNextcloudApp

        owner = user_id or current_user
        nc = AsyncNextcloudApp(user=owner)
        nodes = await nc.files.listdir(path.strip("/"), depth=1)
        return {
            "user_id": owner,
            "path": path,
            "items": [
                {"name": node.name, "path": "/" + node.user_path.strip("/"), "file_id": node.info.fileid}
                for node in nodes
                if node.is_dir
            ],
        }
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Не удалось получить список папок: {exc}") from exc


@router.get("/system/status")
async def system_status(
    _user: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    await session.scalar(select(func.count(Campaign.id)))
    settings = get_settings()
    return {
        "status": "ok",
        "database": "connected",
        "nextcloud_runtime": in_nextcloud_runtime(),
        "reconcile_interval_minutes": settings.reconcile_interval_minutes,
        "admin_groups": sorted(settings.admin_group_set),
        "observer_groups": sorted(settings.observer_group_set),
    }
