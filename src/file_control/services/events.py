from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any

from sqlalchemy import or_, select
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
)
from .classifier import apply_classification, classify, normalize_path, relative_to_campaign

LOGGER = logging.getLogger(__name__)

EVENT_MAP = {
    "NodeCreatedEvent": EventType.CREATED,
    "NodeWrittenEvent": EventType.WRITTEN,
    "NodeRenamedEvent": EventType.RENAMED,
    "NodeDeletedEvent": EventType.DELETED,
    "NodeCopiedEvent": EventType.COPIED,
}

FULL_EVENT_CLASSES = {name: f"OCP\\Files\\Events\\Node\\{name}" for name in EVENT_MAP}

USER_FILE_RE = re.compile(r"^/?(?P<user>[^/]+)/files/(?P<path>.*)$")


def utcnow() -> datetime:
    return datetime.now(UTC)


def _timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            pass
    return utcnow()


@dataclass(slots=True)
class NodeSnapshot:
    owner_user_id: str
    path: str
    file_id: str | None = None
    etag: str = ""
    mime_type: str = "application/octet-stream"
    size: int = 0
    mtime: datetime | None = None
    is_dir: bool = False

    @property
    def filename(self) -> str:
        return PurePosixPath(self.path).name

    @property
    def directory(self) -> str:
        parent = str(PurePosixPath(self.path).parent)
        return "/" if parent == "." else normalize_path(parent)


@dataclass(slots=True)
class NormalizedEvent:
    subtype: str
    event_type: EventType
    target: NodeSnapshot
    source: NodeSnapshot | None
    occurred_at: datetime
    actor_user_id: str
    raw: dict[str, Any]


def _legacy_node(data: dict[str, Any]) -> NodeSnapshot:
    directory = str(data.get("directory") or "/")
    name = str(data.get("name") or "")
    path = normalize_path(f"{directory.rstrip('/')}/{name}")
    file_type = str(data.get("fileType") or data.get("file_type") or "file").lower()
    return NodeSnapshot(
        owner_user_id=str(data.get("userId") or data.get("user_id") or ""),
        path=path,
        file_id=str(data.get("fileId") or data.get("file_id") or "") or None,
        etag=str(data.get("etag") or "").strip('"'),
        mime_type=str(data.get("mime") or "application/octet-stream"),
        size=int(data.get("size") or 0),
        mtime=_timestamp(data.get("mtime")) if data.get("mtime") else None,
        is_dir=file_type == "dir",
    )


def _webhook_path_node(data: dict[str, Any], fallback_user: str = "") -> NodeSnapshot:
    raw_path = str(data.get("path") or "")
    match = USER_FILE_RE.match(raw_path)
    if match:
        owner = match.group("user")
        path = normalize_path(match.group("path"))
    else:
        owner = fallback_user
        path = normalize_path(raw_path)
    return NodeSnapshot(
        owner_user_id=owner,
        path=path,
        file_id=str(data.get("id") or "") or None,
    )


async def normalize_event(payload: dict[str, Any]) -> NormalizedEvent:
    if payload.get("event_type") == "node_event":
        subtype = str(payload.get("event_subtype") or "")
        if subtype not in EVENT_MAP:
            raise ValueError(f"Неподдерживаемое событие: {subtype}")
        event_data = payload.get("event_data") or {}
        target = _legacy_node(event_data.get("target") or {})
        source_data = event_data.get("source")
        source = _legacy_node(source_data) if source_data else None
        actor = target.owner_user_id
        return NormalizedEvent(subtype, EVENT_MAP[subtype], target, source, utcnow(), actor, payload)

    envelope = payload.get("event") or {}
    event_class = str(envelope.get("class") or "")
    subtype = event_class.rsplit("\\", 1)[-1]
    if subtype not in EVENT_MAP:
        raise ValueError(f"Неподдерживаемое событие: {event_class or 'не указано'}")
    user = payload.get("user") or {}
    actor = str(user.get("uid") or "")
    if subtype in {"NodeRenamedEvent", "NodeCopiedEvent"}:
        target = _webhook_path_node(envelope.get("target") or {}, actor)
        source = _webhook_path_node(envelope.get("source") or {}, actor)
    else:
        target = _webhook_path_node(envelope.get("node") or {}, actor)
        source = None
    event = NormalizedEvent(
        subtype, EVENT_MAP[subtype], target, source, _timestamp(payload.get("time")), actor, payload
    )
    if event.event_type != EventType.DELETED:
        event.target = await enrich_snapshot(event.target)
    return event


async def enrich_snapshot(snapshot: NodeSnapshot) -> NodeSnapshot:
    if not snapshot.owner_user_id:
        return snapshot
    try:
        from nc_py_api import AsyncNextcloudApp

        nc = AsyncNextcloudApp(user=snapshot.owner_user_id)
        node = await nc.files.by_id(snapshot.file_id) if snapshot.file_id else None
        if node is None:
            node = await nc.files.by_path(snapshot.path.lstrip("/"))
        if node is None:
            return snapshot
        return NodeSnapshot(
            owner_user_id=snapshot.owner_user_id,
            path=normalize_path(node.user_path),
            file_id=str(node.info.fileid or snapshot.file_id or "") or None,
            etag=node.etag_unquoted,
            mime_type=node.info.mimetype or "application/octet-stream",
            size=int(node.info.content_length or node.info.size or 0),
            mtime=node.info.last_modified,
            is_dir=node.is_dir,
        )
    except Exception as exc:
        LOGGER.warning("Не удалось дочитать метаданные узла %s: %s", snapshot.path, exc)
        return snapshot


async def _active_campaigns(session: AsyncSession, owner_user_id: str) -> list[Campaign]:
    return list(
        (
            await session.scalars(
                select(Campaign)
                .where(Campaign.status == CampaignStatus.ACTIVE, Campaign.storage_user_id == owner_user_id)
                .options(
                    selectinload(Campaign.organizations).selectinload(CampaignOrganization.organization),
                    selectinload(Campaign.dimensions).selectinload(Dimension.values),
                )
            )
        ).all()
    )


def _event_key(campaign_id: int, event: NormalizedEvent, snapshot: NodeSnapshot, old_path: str = "") -> str:
    identity = {
        "campaign": campaign_id,
        "type": event.event_type.value,
        "file_id": snapshot.file_id,
        "etag": snapshot.etag,
        "path": snapshot.path,
        "old_path": old_path,
    }
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


async def _find_file(session: AsyncSession, campaign_id: int, snapshot: NodeSnapshot) -> FileRecord | None:
    predicates = [FileRecord.full_path == snapshot.path]
    if snapshot.file_id:
        predicates.insert(0, FileRecord.nextcloud_file_id == snapshot.file_id)
    return await session.scalar(
        select(FileRecord).where(FileRecord.campaign_id == campaign_id, or_(*predicates)).order_by(FileRecord.id.desc())
    )


async def _record_event(
    session: AsyncSession,
    file: FileRecord,
    event: NormalizedEvent,
    snapshot: NodeSnapshot,
    old_path: str = "",
) -> bool:
    key = _event_key(file.campaign_id, event, snapshot, old_path)
    if await session.scalar(select(FileEvent.id).where(FileEvent.event_key == key)):
        return False
    session.add(
        FileEvent(
            file_id=file.id,
            event_key=key,
            event_type=event.event_type,
            occurred_at=event.occurred_at,
            old_path=old_path,
            new_path=snapshot.path,
            etag=snapshot.etag,
            size=snapshot.size,
            actor_user_id=event.actor_user_id,
            raw_event=event.raw,
        )
    )
    return True


async def _upsert_file(
    session: AsyncSession,
    campaign: Campaign,
    event: NormalizedEvent,
    snapshot: NodeSnapshot,
    old_path: str = "",
) -> bool:
    if snapshot.is_dir:
        return False
    file = await _find_file(session, campaign.id, snapshot)
    is_new = file is None
    if file is None:
        file = FileRecord(
            campaign_id=campaign.id,
            nextcloud_file_id=snapshot.file_id,
            owner_user_id=snapshot.owner_user_id,
            filename=snapshot.filename,
            directory=snapshot.directory,
            relative_path="",
            full_path=snapshot.path,
            mime_type=snapshot.mime_type,
            size=snapshot.size,
            etag=snapshot.etag,
            first_received_at=event.occurred_at,
            last_received_at=event.occurred_at,
            node_mtime=snapshot.mtime,
            event_received_at=utcnow(),
            revision_count=0,
            discovered_by="event",
        )
        session.add(file)
        await session.flush()
    if not await _record_event(session, file, event, snapshot, old_path):
        return False

    previous_etag = file.etag
    file.nextcloud_file_id = snapshot.file_id or file.nextcloud_file_id
    file.owner_user_id = snapshot.owner_user_id or file.owner_user_id
    file.filename = snapshot.filename
    file.directory = snapshot.directory
    file.full_path = snapshot.path
    file.mime_type = snapshot.mime_type or file.mime_type
    file.size = snapshot.size
    file.etag = snapshot.etag or file.etag
    file.node_mtime = snapshot.mtime or file.node_mtime
    file.event_received_at = utcnow()
    file.deleted = False
    file.deleted_at = None
    if event.event_type == EventType.WRITTEN and not is_new and (not snapshot.etag or snapshot.etag != previous_etag):
        file.revision_count += 1
        file.last_received_at = event.occurred_at
    elif event.event_type in {EventType.CREATED, EventType.COPIED} and not is_new:
        file.last_received_at = event.occurred_at
    await apply_classification(session, file, classify(campaign, snapshot.path))
    return True


async def _mark_deleted_or_moved(
    session: AsyncSession,
    campaign: Campaign,
    event: NormalizedEvent,
    snapshot: NodeSnapshot,
    new_path: str = "",
) -> int:
    path = normalize_path(snapshot.path)
    predicates = [FileRecord.full_path == path, FileRecord.full_path.like(path.rstrip("/") + "/%")]
    if snapshot.file_id:
        predicates.insert(0, FileRecord.nextcloud_file_id == snapshot.file_id)
    files = list(
        (
            await session.scalars(
                select(FileRecord).where(
                    FileRecord.campaign_id == campaign.id,
                    FileRecord.deleted.is_(False),
                    or_(*predicates),
                )
            )
        ).all()
    )
    changed = 0
    for file in files:
        event_snapshot = NodeSnapshot(
            owner_user_id=file.owner_user_id,
            path=new_path or file.full_path,
            file_id=file.nextcloud_file_id,
            etag=file.etag,
            mime_type=file.mime_type,
            size=file.size,
            mtime=file.node_mtime,
        )
        if not await _record_event(session, file, event, event_snapshot, old_path=file.full_path):
            continue
        file.deleted = True
        file.deleted_at = event.occurred_at
        file.event_received_at = utcnow()
        changed += 1
    return changed


async def process_event_payload(payload: dict[str, Any]) -> dict[str, Any]:
    event = await normalize_event(payload)
    if not event.target.owner_user_id:
        raise ValueError("В событии не удалось определить владельца файла")
    changed = 0
    async with SessionFactory() as session:
        target_campaigns = await _active_campaigns(session, event.target.owner_user_id)
        target_matches = [
            campaign
            for campaign in target_campaigns
            if relative_to_campaign(event.target.path, campaign.root_path) is not None
        ]

        if event.event_type == EventType.DELETED:
            for campaign in target_matches:
                changed += await _mark_deleted_or_moved(session, campaign, event, event.target)
        elif event.event_type == EventType.RENAMED and event.source:
            source_campaigns = await _active_campaigns(session, event.source.owner_user_id)
            source_matches = [
                campaign
                for campaign in source_campaigns
                if relative_to_campaign(event.source.path, campaign.root_path) is not None
            ]
            target_ids = {campaign.id for campaign in target_matches}
            for campaign in source_matches:
                if campaign.id not in target_ids:
                    changed += await _mark_deleted_or_moved(
                        session, campaign, event, event.source, new_path=event.target.path
                    )
            for campaign in target_matches:
                changed += int(await _upsert_file(session, campaign, event, event.target, event.source.path))
        else:
            for campaign in target_matches:
                changed += int(await _upsert_file(session, campaign, event, event.target))
        await session.commit()
    LOGGER.info("Обработано событие %s, изменено записей: %d", event.subtype, changed)
    return {"accepted": True, "event": event.subtype, "changed": changed}
