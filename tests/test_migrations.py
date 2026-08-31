from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from file_control.migrations import migrate_database


@pytest.mark.asyncio
async def test_legacy_campaign_data_moves_to_watch_tables(tmp_path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy.sqlite3'}")
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """
                CREATE TABLE schema_migrations (
                    version INTEGER PRIMARY KEY, name VARCHAR(255) NOT NULL, applied_at DATETIME NOT NULL
                )
                """
            )
        )
        await connection.execute(
            text(
                """
                INSERT INTO schema_migrations VALUES (1, 'initial_schema', '2026-01-01 00:00:00')
                """
            )
        )
        await connection.execute(
            text(
                """
                CREATE TABLE campaigns (
                    id INTEGER PRIMARY KEY, name VARCHAR(500), root_path VARCHAR(2048),
                    storage_user_id VARCHAR(255), start_at DATETIME, deadline_at DATETIME,
                    status VARCHAR(30), created_at DATETIME, updated_at DATETIME, created_by VARCHAR(255)
                )
                """
            )
        )
        await connection.execute(
            text(
                """
                CREATE TABLE file_records (
                    id INTEGER PRIMARY KEY, campaign_id INTEGER, nextcloud_file_id VARCHAR(255),
                    filename VARCHAR(1024), full_path VARCHAR(4096), relative_path VARCHAR(2048),
                    directory VARCHAR(2048), mime_type VARCHAR(255), size INTEGER, etag VARCHAR(512),
                    first_received_at DATETIME, last_received_at DATETIME, node_mtime DATETIME,
                    deleted BOOLEAN, deleted_at DATETIME, revision_count INTEGER, discovered_by VARCHAR(30)
                )
                """
            )
        )
        await connection.execute(
            text(
                """
                CREATE TABLE file_events (
                    id INTEGER PRIMARY KEY, file_id INTEGER, event_key VARCHAR(64),
                    event_type VARCHAR(40), occurred_at DATETIME, old_path VARCHAR(4096),
                    new_path VARCHAR(4096), size INTEGER, etag VARCHAR(512),
                    actor_user_id VARCHAR(255), raw_event JSON
                )
                """
            )
        )
        await connection.execute(text("CREATE INDEX ix_event_file_time ON file_events (file_id, occurred_at)"))
        await connection.execute(
            text(
                """
                INSERT INTO campaigns VALUES (
                    7, 'Олимпиада', '/Олимпиада', 'collector', NULL, NULL, 'ACTIVE',
                    '2026-01-01 00:00:00', '2026-01-01 00:00:00', 'admin'
                )
                """
            )
        )
        await connection.execute(
            text(
                """
                INSERT INTO file_records VALUES (
                    9, 7, '42', 'result.xlsx', '/Олимпиада/525/result.xlsx',
                    '525/result.xlsx', '/Олимпиада/525', 'application/test', 123, 'v1',
                    '2026-01-02 10:00:00', '2026-01-02 10:00:00', '2020-01-01 00:00:00',
                    0, NULL, 0, 'event'
                )
                """
            )
        )
        await connection.execute(
            text(
                """
                INSERT INTO file_events VALUES (
                    11, 9, 'legacy-key', 'CREATED', '2026-01-02 10:00:00', '',
                    '/Олимпиада/525/result.xlsx', 123, 'v1', 'collector', '{}'
                )
                """
            )
        )

    await migrate_database(engine)
    async with engine.connect() as connection:
        watch = (
            await connection.execute(text("SELECT name, root_path, status, scope_type, group_ids FROM watches"))
        ).one()
        file = (await connection.execute(text('SELECT watch_id, relative_path, "exists" FROM watch_files'))).one()
        event = (await connection.execute(text("SELECT watch_id, event_type, path FROM watch_events"))).one()
        activity = (
            await connection.execute(text("SELECT watch_id, event_type, node_type, path FROM watch_activity_events"))
        ).one()
    await engine.dispose()

    assert watch == ("Олимпиада", "/Олимпиада", "ACTIVE", "personal", "[]")
    assert file == (7, "525/result.xlsx", 1)
    assert event == (7, "CREATED", "/Олимпиада/525/result.xlsx")
    assert activity == (7, "CREATED", "file", "/Олимпиада/525/result.xlsx")


@pytest.mark.asyncio
async def test_activity_migration_merges_reconciliation_and_webhook_duplicates(tmp_path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'duplicates.sqlite3'}")
    await migrate_database(engine)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """
                INSERT INTO watches
                    (id, name, root_path, owner_user_id, scope_type, group_ids, status,
                     created_at, updated_at, created_by)
                VALUES
                    (1, 'Test', '/Test', 'admin', 'personal', '[]', 'ACTIVE',
                     '2026-01-01 00:00:00', '2026-01-01 00:00:00', 'admin')
                """
            )
        )
        await connection.execute(
            text(
                """
                INSERT INTO watch_files
                    (id, watch_id, nextcloud_file_id, filename, full_path, relative_path,
                     parent_path, mime_type, extension, size, etag, first_seen_at,
                     last_seen_at, "exists", revision_count, discovered_by)
                VALUES
                    (1, 1, '42', 'result.xlsx', '/Test/result.xlsx', 'result.xlsx', '',
                     'application/test', 'xlsx', 10, 'v1', '2026-01-01 10:00:00',
                     '2026-01-01 10:00:00', 1, 0, 'reconciliation')
                """
            )
        )
        for event_id, source in ((1, "reconciliation"), (2, "webhook")):
            await connection.execute(
                text(
                    """
                    INSERT INTO watch_activity_events
                        (id, watch_id, file_id, event_key, event_type, node_type, event_time,
                         path, old_path, new_path, size, etag, actor_user_id, raw_event)
                    VALUES
                        (:id, 1, 1, :key, 'CREATED', 'file', '2026-01-01 10:00:00',
                         '/Test/result.xlsx', '', '/Test/result.xlsx', 10, 'v1', 'admin', :raw)
                    """
                ),
                {"id": event_id, "key": f"old-{event_id}", "raw": f'{{"source":"{source}"}}'},
            )
        await connection.execute(text("DELETE FROM schema_migrations WHERE version = 4"))

    await migrate_database(engine)
    async with engine.connect() as connection:
        count = await connection.scalar(text("SELECT COUNT(*) FROM watch_activity_events"))
        key = await connection.scalar(text("SELECT event_key FROM watch_activity_events"))
    await engine.dispose()

    assert count == 1
    assert len(key) == 64


@pytest.mark.asyncio
async def test_operational_migration_restores_known_update_activity(tmp_path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'updates.sqlite3'}")
    await migrate_database(engine)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """
                INSERT INTO watches
                    (id, name, root_path, owner_user_id, scope_type, group_ids, status,
                     created_at, updated_at, created_by)
                VALUES
                    (1, 'Test', '/Test', 'admin', 'personal', '[]', 'ACTIVE',
                     '2026-01-01 00:00:00', '2026-01-01 00:00:00', 'admin')
                """
            )
        )
        await connection.execute(
            text(
                """
                INSERT INTO watch_files
                    (id, watch_id, nextcloud_file_id, filename, full_path, relative_path,
                     parent_path, mime_type, extension, size, etag, first_seen_at,
                     last_seen_at, "exists", revision_count, discovered_by)
                VALUES
                    (1, 1, '42', 'result.xlsx', '/Test/result.xlsx', 'result.xlsx', '',
                     'application/test', 'xlsx', 10, 'v2', '2026-01-01 10:00:00',
                     '2026-01-02 10:00:00', 1, 2, 'reconciliation')
                """
            )
        )
        await connection.execute(text("DELETE FROM schema_migrations WHERE version = 5"))

    await migrate_database(engine)
    async with engine.connect() as connection:
        event = (
            await connection.execute(
                text("SELECT event_type, path, actor_user_id FROM watch_activity_events WHERE file_id = 1")
            )
        ).one()
    await engine.dispose()

    assert event == ("UPDATED", "/Test/result.xlsx", "migration")
