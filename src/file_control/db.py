from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import MetaData, event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .config import get_settings

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


settings = get_settings()
engine: AsyncEngine = create_async_engine(
    settings.async_database_url,
    pool_pre_ping=True,
    echo=settings.debug,
)
SessionFactory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


if settings.async_database_url.startswith("sqlite+"):

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()


async def init_database() -> None:
    from . import models  # noqa: F401
    from .migrations import migrate_database

    await migrate_database(engine)


async def close_database() -> None:
    await engine.dispose()


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionFactory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
