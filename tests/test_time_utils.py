from __future__ import annotations

from datetime import UTC, datetime

from file_control.services.reporting import _csv_value, human_size
from file_control.time_utils import api_datetime, as_local_naive


def test_naive_sqlite_utc_is_marked_as_utc_for_api() -> None:
    stored = datetime(2026, 8, 25, 11, 21, 57)

    assert api_datetime(stored) == "2026-08-25T14:21:57+03:00"


def test_report_time_is_converted_to_moscow() -> None:
    stored = datetime(2026, 8, 25, 11, 21, tzinfo=UTC)

    assert as_local_naive(stored) == datetime(2026, 8, 25, 14, 21)
    assert _csv_value(stored) == "25.08.2026 14:21"


def test_report_sizes_are_human_readable() -> None:
    assert human_size(178) == "178 Б"
    assert human_size(7009) == "6,8 КБ"
    assert human_size(36_028) == "35 КБ"
