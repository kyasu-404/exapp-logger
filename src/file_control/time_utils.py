from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import get_settings


def as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def api_datetime(value: datetime | None) -> str | None:
    normalized = as_utc(value)
    return normalized.astimezone(local_timezone()).isoformat() if normalized else None


def local_timezone() -> tzinfo:
    name = get_settings().app_timezone
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        if name == "Europe/Moscow":
            return timezone(timedelta(hours=3), name="MSK")
        raise


def as_local_naive(value: datetime | None) -> datetime | None:
    normalized = as_utc(value)
    return normalized.astimezone(local_timezone()).replace(tzinfo=None) if normalized else None


def start_of_local_day_utc() -> datetime:
    now = datetime.now(local_timezone())
    local_midnight = datetime.combine(now.date(), time.min, tzinfo=local_timezone())
    return local_midnight.astimezone(UTC)


def local_day_bounds_utc(value: date) -> tuple[datetime, datetime]:
    zone = local_timezone()
    start = datetime.combine(value, time.min, tzinfo=zone)
    end = datetime.combine(value, time.max, tzinfo=zone)
    return start.astimezone(UTC), end.astimezone(UTC)


def local_datetime_utc(value: date, clock: time) -> datetime:
    return datetime.combine(value, clock, tzinfo=local_timezone()).astimezone(UTC)


def local_today() -> str:
    return datetime.now(local_timezone()).date().isoformat()
