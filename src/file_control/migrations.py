from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, insert, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

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


MIGRATIONS: tuple[Migration, ...] = ((1, "initial_schema", _initial_schema),)


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
