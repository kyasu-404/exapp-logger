from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .models import ReportCadence, WatchStatus


class APIModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, use_enum_values=True)


def normalize_path(value: str) -> str:
    normalized = "/" + value.strip().replace("\\", "/").strip("/")
    if normalized == "/":
        raise ValueError("Нельзя наблюдать корень всего хранилища")
    return normalized


class WatchCreate(APIModel):
    name: str = Field(min_length=1, max_length=500)
    root_path: str = Field(min_length=1, max_length=4096)
    owner_user_id: str | None = Field(default=None, max_length=255)
    scope_type: Literal["personal", "team"] = "personal"
    group_ids: list[str] = Field(default_factory=list, max_length=200)
    started_at: datetime | None = None
    ended_at: datetime | None = None
    status: WatchStatus = WatchStatus.ACTIVE

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        return value.strip()

    @field_validator("root_path")
    @classmethod
    def normalize_root(cls, value: str) -> str:
        return normalize_path(value)

    @model_validator(mode="after")
    def valid_period(self):
        if self.started_at and self.ended_at and self.ended_at < self.started_at:
            raise ValueError("Дата окончания не может быть раньше даты начала")
        self.group_ids = sorted({item.strip() for item in self.group_ids if item.strip()})
        return self


class WatchUpdate(APIModel):
    name: str | None = Field(default=None, min_length=1, max_length=500)
    root_path: str | None = Field(default=None, min_length=1, max_length=4096)
    owner_user_id: str | None = Field(default=None, max_length=255)
    scope_type: Literal["personal", "team"] | None = None
    group_ids: list[str] | None = Field(default=None, max_length=200)
    started_at: datetime | None = None
    ended_at: datetime | None = None
    status: WatchStatus | None = None

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @field_validator("root_path")
    @classmethod
    def normalize_root(cls, value: str | None) -> str | None:
        return normalize_path(value) if value is not None else None

    @field_validator("group_ids")
    @classmethod
    def normalize_groups(cls, value: list[str] | None) -> list[str] | None:
        return sorted({item.strip() for item in value if item.strip()}) if value is not None else None


class Page(APIModel):
    items: list[Any]
    page: int
    page_size: int
    total: int
    pages: int


class ReportScheduleUpdate(APIModel):
    enabled: bool = False
    cadence: ReportCadence = ReportCadence.DAILY
    local_time: str = Field(default="09:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    weekday: int = Field(default=0, ge=0, le=6)
    day_of_month: int = Field(default=1, ge=1, le=28)
    destination_path: str = Field(default="", max_length=4096)

    @field_validator("destination_path")
    @classmethod
    def normalize_destination(cls, value: str) -> str:
        if not value.strip():
            return ""
        return "/" + value.strip().replace("\\", "/").strip("/")

    @model_validator(mode="after")
    def enabled_schedule_has_destination(self):
        if self.enabled and not self.destination_path:
            raise ValueError("Выберите папку Nextcloud для автоматических отчётов")
        return self
