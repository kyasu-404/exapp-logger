from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_id: str = Field(default="file_control", validation_alias="APP_ID")
    app_version: str = Field(default="2.3.1", validation_alias="APP_VERSION")
    app_display_name: str = Field(default="Отчёт сдачи", validation_alias="APP_DISPLAY_NAME")
    app_persistent_storage: str = Field(default="./data", validation_alias="APP_PERSISTENT_STORAGE")
    database_url: str | None = Field(default=None, validation_alias="DATABASE_URL")
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    debug: bool = Field(default=False, validation_alias="DEBUG")
    app_timezone: str = Field(default="Europe/Moscow", validation_alias="APP_TIMEZONE")
    reconcile_interval_minutes: int = Field(default=45, ge=5, le=1440, validation_alias="RECONCILE_INTERVAL_MINUTES")
    admin_groups: str = Field(default="admin,file_control_admins", validation_alias="ADMIN_GROUPS")
    observer_groups: str = Field(default="file_control_observers", validation_alias="OBSERVER_GROUPS")

    @property
    def storage_path(self) -> Path:
        path = Path(self.app_persistent_storage).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def async_database_url(self) -> str:
        raw = self.database_url
        if not raw:
            sqlite_path = (self.storage_path / "file_control.sqlite3").as_posix()
            return f"sqlite+aiosqlite:///{sqlite_path}"
        if raw.startswith("postgres://"):
            return raw.replace("postgres://", "postgresql+asyncpg://", 1)
        if raw.startswith("postgresql://"):
            return raw.replace("postgresql://", "postgresql+asyncpg://", 1)
        if raw.startswith("sqlite://") and not raw.startswith("sqlite+aiosqlite://"):
            return raw.replace("sqlite://", "sqlite+aiosqlite://", 1)
        return raw

    @property
    def admin_group_set(self) -> set[str]:
        return {item.strip() for item in self.admin_groups.split(",") if item.strip()}

    @property
    def observer_group_set(self) -> set[str]:
        return {item.strip() for item in self.observer_groups.split(",") if item.strip()}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def in_nextcloud_runtime() -> bool:
    return bool(os.getenv("APP_SECRET") and os.getenv("NEXTCLOUD_URL"))
