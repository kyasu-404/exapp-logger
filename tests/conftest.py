from __future__ import annotations

import os

import pytest_asyncio

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("APP_PERSISTENT_STORAGE", "./.test-data")

from file_control.db import Base, engine  # noqa: E402


@pytest_asyncio.fixture(autouse=True)
async def clean_database():
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    yield
