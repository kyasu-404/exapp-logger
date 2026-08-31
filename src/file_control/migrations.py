from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, insert, inspect, select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from . import models as _models  # noqa: F401  # populate Base.metadata for standalone migrations
from .activity_keys import activity_event_key
from .db import Base

LOGGER = logging.getLogger(__name__)
MIGRATION_METADATA = MetaData()
SCHEMA_MIGRATIONS = Table(
    "schema_migrations",
    MIGRATION_METADATA,
    Column("version", Integer, primary_key=True),
    Column("name", String(255), nullable=False),
    Column("applied_at", DateTime(timezone=True), nullable=False),
)
Migration = tuple[int, str, Callable[[AsyncConnection], Awaitable[None]]]


async def _initial_schema(connection: AsyncConnection) -> None:
    await connection.run_sync(Base.metadata.create_all)


async def _simplified_watch_schema(connection: AsyncConnection) -> None:
    """Create the path-based schema and preserve useful records from 1.0.x.

    The former campaign/organization tables are intentionally left untouched as
    a recoverable archive. The application only uses the new watch tables.
    """
    await connection.run_sync(Base.metadata.create_all)
    tables = await connection.run_sync(lambda sync_connection: set(inspect(sync_connection).get_table_names()))
    if "campaigns" not in tables:
        return
    if await connection.scalar(text("SELECT COUNT(*) FROM watches")):
        return

    await connection.execute(
        text(
            """
            INSERT INTO watches
                (id, name, root_path, owner_user_id, scope_type, group_ids, started_at, ended_at, status,
                 created_at, updated_at, created_by)
            SELECT id, name, root_path, storage_user_id, 'personal', '[]', start_at, deadline_at,
                   CASE LOWER(CAST(status AS TEXT))
                     WHEN 'draft' THEN 'DRAFT'
                     WHEN 'active' THEN 'ACTIVE'
                     WHEN 'completed' THEN 'COMPLETED'
                     WHEN 'archived' THEN 'ARCHIVED'
                     ELSE 'DRAFT'
                   END,
                   created_at, updated_at, created_by
            FROM campaigns
            """
        )
    )

    if "file_records" in tables:
        await connection.execute(
            text(
                """
                INSERT INTO watch_files
                    (id, watch_id, nextcloud_file_id, filename, full_path, relative_path,
                     parent_path, mime_type, extension, size, etag, first_seen_at,
                     last_seen_at, nextcloud_upload_time, nextcloud_mtime, "exists",
                     deleted_at, revision_count, discovered_by)
                SELECT id, campaign_id, nextcloud_file_id, filename, full_path,
                       CASE WHEN relative_path = '' THEN full_path ELSE relative_path END,
                       directory, mime_type, '', size, etag, first_received_at,
                       last_received_at, NULL, node_mtime,
                       CASE WHEN deleted THEN FALSE ELSE TRUE END,
                       deleted_at, revision_count, discovered_by
                FROM file_records
                WHERE campaign_id IN (SELECT id FROM watches)
                """
            )
        )

    if "file_events" in tables and "file_records" in tables:
        await connection.execute(
            text(
                """
                INSERT INTO watch_events
                    (id, watch_id, file_id, event_key, event_type, event_time, path,
                     old_path, new_path, size, etag, actor_user_id, raw_event)
                SELECT event.id, file.campaign_id, event.file_id, event.event_key,
                       CASE LOWER(CAST(event.event_type AS TEXT))
                         WHEN 'created' THEN 'CREATED'
                         WHEN 'written' THEN 'UPDATED'
                         WHEN 'renamed' THEN 'MOVED'
                         WHEN 'deleted' THEN 'DELETED'
                         WHEN 'copied' THEN 'COPIED'
                         WHEN 'reconciled_created' THEN 'CREATED'
                         WHEN 'reconciled_updated' THEN 'UPDATED'
                         WHEN 'reconciled_missing' THEN 'DELETED'
                         ELSE 'UPDATED'
                       END,
                       event.occurred_at,
                       CASE WHEN event.new_path <> '' THEN event.new_path ELSE event.old_path END,
                       event.old_path, event.new_path, event.size, event.etag,
                       event.actor_user_id, event.raw_event
                FROM file_events event
                JOIN file_records file ON file.id = event.file_id
                WHERE file.campaign_id IN (SELECT id FROM watches)
                """
            )
        )

    if connection.dialect.name == "postgresql":
        for table_name in ("watches", "watch_files", "watch_events"):
            await connection.execute(
                text(
                    f"SELECT setval(pg_get_serial_sequence('{table_name}', 'id'), "
                    f"COALESCE((SELECT MAX(id) FROM {table_name}), 1), "
                    f"EXISTS(SELECT 1 FROM {table_name}))"
                )
            )


async def _team_scope_activity_schema(connection: AsyncConnection) -> None:
    """Add team-folder scope and a folder-capable, user-visible activity stream."""
    await connection.run_sync(Base.metadata.create_all)
    columns = await connection.run_sync(
        lambda sync_connection: {item["name"] for item in inspect(sync_connection).get_columns("watches")}
    )
    if "scope_type" not in columns:
        await connection.execute(
            text("ALTER TABLE watches ADD COLUMN scope_type VARCHAR(20) NOT NULL DEFAULT 'personal'")
        )
    if "group_ids" not in columns:
        if connection.dialect.name == "postgresql":
            statement = "ALTER TABLE watches ADD COLUMN group_ids JSON NOT NULL DEFAULT '[]'::json"
        else:
            statement = "ALTER TABLE watches ADD COLUMN group_ids JSON NOT NULL DEFAULT '[]'"
        await connection.execute(text(statement))

    # Preserve all useful historical activity, but deliberately omit content
    # update entries: current file metadata still records those changes.
    await connection.execute(
        text(
            """
            INSERT INTO watch_activity_events
                (watch_id, file_id, event_key, event_type, node_type, event_time,
                 path, old_path, new_path, size, etag, actor_user_id, raw_event)
            SELECT watch_id, file_id, event_key, event_type, 'file', event_time,
                   path, old_path, new_path, size, etag, actor_user_id, raw_event
            FROM watch_events
            WHERE LOWER(CAST(event_type AS TEXT)) <> 'updated'
            """
        )
    )


def _raw_node_id(raw_event) -> str:
    if isinstance(raw_event, str):
        try:
            raw_event = json.loads(raw_event)
        except json.JSONDecodeError:
            return ""
    if not isinstance(raw_event, dict):
        return ""
    envelope = raw_event.get("event") or {}
    for key in ("node", "target", "source"):
        node_id = (envelope.get(key) or {}).get("id")
        if node_id:
            return str(node_id)
    event_data = raw_event.get("event_data") or {}
    for key in ("target", "source"):
        node_id = (event_data.get(key) or {}).get("fileId") or (event_data.get(key) or {}).get("file_id")
        if node_id:
            return str(node_id)
    return ""


async def _canonical_activity_keys(connection: AsyncConnection) -> None:
    """Merge activity duplicated by reconciliation and delayed webhook delivery."""
    await connection.run_sync(Base.metadata.create_all)
    rows = (
        (
            await connection.execute(
                text(
                    """
                SELECT activity.id, activity.watch_id, activity.file_id, activity.event_type,
                       activity.event_time, activity.path, activity.old_path, activity.new_path,
                       activity.raw_event, file.nextcloud_file_id
                FROM watch_activity_events activity
                LEFT JOIN watch_files file ON file.id = activity.file_id
                ORDER BY activity.id
                """
                )
            )
        )
        .mappings()
        .all()
    )

    retained: dict[str, int] = {}
    duplicates: list[int] = []
    updates: list[tuple[int, str]] = []
    for row in rows:
        node_identity = str(row["nextcloud_file_id"] or "") or _raw_node_id(row["raw_event"])
        if not node_identity and row["file_id"]:
            node_identity = f"record:{row['file_id']}"
        path = row["path"] or row["new_path"] or row["old_path"] or "/"
        key = activity_event_key(
            row["watch_id"],
            row["event_type"],
            node_identity,
            path,
            row["old_path"] or "",
            row["event_time"],
        )
        if key in retained:
            duplicates.append(row["id"])
        else:
            retained[key] = row["id"]
            updates.append((row["id"], key))

    for event_id in duplicates:
        await connection.execute(
            text("DELETE FROM watch_activity_events WHERE id = :event_id"),
            {"event_id": event_id},
        )
    for event_id, key in updates:
        await connection.execute(
            text("UPDATE watch_activity_events SET event_key = :key WHERE id = :event_id"),
            {"event_id": event_id, "key": key},
        )
    if duplicates:
        LOGGER.info("Удалено дублирующихся событий активности: %d", len(duplicates))


async def _operational_reporting_schema(connection: AsyncConnection) -> None:
    """Add folder state, report schedules and live diagnostics without losing data."""
    watch_columns = await connection.run_sync(
        lambda sync_connection: {item["name"] for item in inspect(sync_connection).get_columns("watches")}
    )
    if "last_webhook_at" not in watch_columns:
        column_type = "TIMESTAMP WITH TIME ZONE" if connection.dialect.name == "postgresql" else "DATETIME"
        await connection.execute(text(f"ALTER TABLE watches ADD COLUMN last_webhook_at {column_type}"))
    await connection.run_sync(Base.metadata.create_all)

    # Older releases counted content revisions but deliberately hid them from
    # Activity. Preserve that knowledge as one explicit historical event per
    # revised file; future revisions are recorded individually by ETag.
    revised_files = (
        (
            await connection.execute(
                text(
                    """
                SELECT file.id, file.watch_id, file.nextcloud_file_id, file.full_path,
                       file.size, file.etag, file.last_seen_at
                FROM watch_files file
                WHERE file.revision_count > 0
                  AND NOT EXISTS (
                      SELECT 1 FROM watch_activity_events activity
                      WHERE activity.file_id = file.id
                        AND LOWER(CAST(activity.event_type AS TEXT)) = 'updated'
                  )
                """
                )
            )
        )
        .mappings()
        .all()
    )
    for file in revised_files:
        event_time = file["last_seen_at"]
        if isinstance(event_time, str):
            event_time = datetime.fromisoformat(event_time.replace("Z", "+00:00"))
        node_identity = f"{file['nextcloud_file_id'] or file['id']}:{file['etag'] or 'legacy-revision'}"
        key = activity_event_key(
            file["watch_id"],
            "updated",
            node_identity,
            file["full_path"],
            "",
            event_time,
        )
        await connection.execute(
            _models.ActivityEvent.__table__.insert().values(
                watch_id=file["watch_id"],
                file_id=file["id"],
                event_key=key,
                event_type=_models.EventType.UPDATED,
                node_type="file",
                event_time=event_time,
                path=file["full_path"],
                old_path="",
                new_path=file["full_path"],
                size=file["size"],
                etag=file["etag"],
                actor_user_id="migration",
                raw_event={"source": "revision_history_migration"},
            )
        )


MIGRATIONS: tuple[Migration, ...] = (
    (1, "initial_schema", _initial_schema),
    (2, "simplified_watch_schema", _simplified_watch_schema),
    (3, "team_scope_activity_schema", _team_scope_activity_schema),
    (4, "canonical_activity_keys", _canonical_activity_keys),
    (5, "operational_reporting_schema", _operational_reporting_schema),
)


async def migrate_database(engine: AsyncEngine) -> None:
    """Apply ordered, transactional database migrations at application startup."""
    async with engine.begin() as connection:
        await connection.run_sync(MIGRATION_METADATA.create_all)
        applied = set((await connection.scalars(select(SCHEMA_MIGRATIONS.c.version))).all())
        for version, name, migration in MIGRATIONS:
            if version in applied:
                continue
            await migration(connection)
            await connection.execute(
                insert(SCHEMA_MIGRATIONS).values(
                    version=version,
                    name=name,
                    applied_at=datetime.now(UTC),
                )
            )
            LOGGER.info("Применена миграция БД %d: %s", version, name)
