from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections import defaultdict
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..db import SessionFactory
from ..models import (
    Campaign,
    CampaignOrganization,
    CampaignStatus,
    Dimension,
    EventType,
    FileEvent,
    FileRecord,
    ReconciliationRun,
)
from .classifier import apply_classification, classify, normalize_path
from .events import NodeSnapshot

LOGGER = logging.getLogger(__name__)
_LOCKS: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


def utcnow() -> datetime:
    return datetime.now(UTC)


def _key(campaign_id: int, event_type: EventType, file_identity: str, etag: str, path: str) -> str:
    raw = json.dumps(
        {"campaign": campaign_id, "type": event_type.value, "file": file_identity, "etag": etag, "path": path},
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def snapshot_from_fs_node(owner: str, node) -> NodeSnapshot:
    return NodeSnapshot(
        owner_user_id=owner,
        path=normalize_path(node.user_path),
        file_id=str(node.info.fileid or "") or None,
        etag=node.etag_unquoted,
        mime_type=node.info.mimetype or "application/octet-stream",
        size=int(node.info.content_length or node.info.size or 0),
        mtime=node.info.last_modified,
        is_dir=node.is_dir,
    )


async def _campaign(session: AsyncSession, campaign_id: int) -> Campaign | None:
    return await session.scalar(
        select(Campaign)
        .where(Campaign.id == campaign_id)
        .options(
            selectinload(Campaign.organizations).selectinload(CampaignOrganization.organization),
            selectinload(Campaign.dimensions).selectinload(Dimension.values),
        )
    )


async def _event(
    session: AsyncSession,
    file: FileRecord,
    event_type: EventType,
    path: str,
    actor: str,
) -> None:
    key = _key(file.campaign_id, event_type, file.nextcloud_file_id or str(file.id), file.etag, path)
    if await session.scalar(select(FileEvent.id).where(FileEvent.event_key == key)):
        return
    session.add(
        FileEvent(
            file_id=file.id,
            event_key=key,
            event_type=event_type,
            occurred_at=utcnow(),
            old_path=file.full_path if event_type == EventType.RECONCILED_MISSING else "",
            new_path=path,
            etag=file.etag,
            size=file.size,
            actor_user_id=actor,
            raw_event={"source": "reconciliation"},
        )
    )


async def reconcile_campaign(campaign_id: int, triggered_by: str = "system") -> dict[str, int | str]:
    async with _LOCKS[campaign_id]:
        async with SessionFactory() as session:
            campaign = await _campaign(session, campaign_id)
            if campaign is None:
                raise ValueError("Кампания не найдена")
            run = ReconciliationRun(campaign_id=campaign.id, triggered_by=triggered_by)
            session.add(run)
            await session.commit()
            await session.refresh(run)

        try:
            from nc_py_api import AsyncNextcloudApp

            nc = AsyncNextcloudApp(user=campaign.storage_user_id)
            nodes = await nc.files.listdir(campaign.root_path.lstrip("/"), depth=-1)
            snapshots = [snapshot_from_fs_node(campaign.storage_user_id, node) for node in nodes if not node.is_dir]
            checked = len(snapshots)
            created = updated = missing = 0
            now = utcnow()
            async with SessionFactory() as session:
                campaign = await _campaign(session, campaign_id)
                existing = list(
                    (await session.scalars(select(FileRecord).where(FileRecord.campaign_id == campaign_id))).all()
                )
                by_id = {file.nextcloud_file_id: file for file in existing if file.nextcloud_file_id}
                by_path = {file.full_path: file for file in existing}
                seen_ids: set[int] = set()

                for snapshot in snapshots:
                    file = by_id.get(snapshot.file_id) if snapshot.file_id else None
                    file = file or by_path.get(snapshot.path)
                    if file is None:
                        file = FileRecord(
                            campaign_id=campaign_id,
                            nextcloud_file_id=snapshot.file_id,
                            owner_user_id=snapshot.owner_user_id,
                            filename=snapshot.filename,
                            directory=snapshot.directory,
                            relative_path="",
                            full_path=snapshot.path,
                            mime_type=snapshot.mime_type,
                            size=snapshot.size,
                            etag=snapshot.etag,
                            first_received_at=now,
                            last_received_at=now,
                            node_mtime=snapshot.mtime,
                            event_received_at=now,
                            discovered_by="reconciliation",
                        )
                        session.add(file)
                        await session.flush()
                        await apply_classification(session, file, classify(campaign, snapshot.path))
                        await _event(session, file, EventType.RECONCILED_CREATED, snapshot.path, triggered_by)
                        created += 1
                    else:
                        was_changed = (
                            file.etag != snapshot.etag
                            or file.size != snapshot.size
                            or file.full_path != snapshot.path
                            or file.deleted
                        )
                        if was_changed:
                            if file.etag and snapshot.etag and file.etag != snapshot.etag:
                                file.revision_count += 1
                                file.last_received_at = now
                            file.nextcloud_file_id = snapshot.file_id or file.nextcloud_file_id
                            file.filename = snapshot.filename
                            file.directory = snapshot.directory
                            file.full_path = snapshot.path
                            file.mime_type = snapshot.mime_type
                            file.size = snapshot.size
                            file.etag = snapshot.etag
                            file.node_mtime = snapshot.mtime
                            file.event_received_at = now
                            file.deleted = False
                            file.deleted_at = None
                            await apply_classification(session, file, classify(campaign, snapshot.path))
                            await _event(session, file, EventType.RECONCILED_UPDATED, snapshot.path, triggered_by)
                            updated += 1
                    seen_ids.add(file.id)

                for file in existing:
                    if file.id in seen_ids or file.deleted:
                        continue
                    file.deleted = True
                    file.deleted_at = now
                    file.event_received_at = now
                    await _event(session, file, EventType.RECONCILED_MISSING, file.full_path, triggered_by)
                    missing += 1

                run = await session.get(ReconciliationRun, run.id)
                run.status = "completed"
                run.finished_at = utcnow()
                run.checked_count = checked
                run.created_count = created
                run.updated_count = updated
                run.missing_count = missing
                await session.commit()
            LOGGER.info(
                "Сверка кампании %s завершена: проверено=%d новых=%d обновлено=%d отсутствует=%d",
                campaign_id,
                checked,
                created,
                updated,
                missing,
            )
            return {
                "status": "completed",
                "checked": checked,
                "created": created,
                "updated": updated,
                "missing": missing,
            }
        except Exception as exc:
            LOGGER.exception("Ошибка сверки кампании %s", campaign_id)
            async with SessionFactory() as session:
                failed_run = await session.get(ReconciliationRun, run.id)
                if failed_run:
                    failed_run.status = "failed"
                    failed_run.finished_at = utcnow()
                    failed_run.error = str(exc)[:4000]
                    await session.commit()
            raise


async def reconcile_all_active() -> None:
    async with SessionFactory() as session:
        ids = list((await session.scalars(select(Campaign.id).where(Campaign.status == CampaignStatus.ACTIVE))).all())
    for campaign_id in ids:
        try:
            await reconcile_campaign(campaign_id)
        except Exception:
            continue
