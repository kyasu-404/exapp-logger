from __future__ import annotations

import csv
import os
import re
import uuid
from collections.abc import Iterable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..config import get_settings
from ..models import Campaign, FileDimensionValue, FileEvent, FileRecord, ParseStatus
from ..serialization import EVENT_LABELS, file_dimensions
from .progress import build_progress, filter_progress

STATUS_LABELS = {
    "submitted": "Сдано",
    "not_submitted": "Не сдано",
    "partial": "Частично",
    "overdue": "Просрочено",
    "deleted": "Удалено",
    "unrecognized": "Не распознано",
}


def safe_filename(value: str) -> str:
    value = re.sub(r"[^\w\-.а-яА-ЯёЁ]+", "_", value, flags=re.UNICODE)
    return value.strip("_.") or "report"


def _temp_report(suffix: str) -> Path:
    directory = get_settings().storage_path / "reports"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{uuid.uuid4().hex}{suffix}"


def _header_cells(sheet, values: Iterable[Any]):
    cells = []
    for value in values:
        cell = WriteOnlyCell(sheet, value=value)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="0082C9")
        cells.append(cell)
    return cells


def _configure_table(sheet, widths: list[int], rows: int, columns: int) -> None:
    sheet.freeze_panes = "A2"
    if rows:
        sheet.auto_filter.ref = f"A1:{get_column_letter(columns)}{rows + 1}"
    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width


def _date_cell(sheet, value: datetime | None):
    if value is None:
        return None
    if value.tzinfo:
        value = value.astimezone(UTC).replace(tzinfo=None)
    cell = WriteOnlyCell(sheet, value=value)
    cell.number_format = "dd.mm.yyyy hh:mm"
    return cell


async def _files(session: AsyncSession, campaign_id: int) -> list[FileRecord]:
    return list(
        (
            await session.scalars(
                select(FileRecord)
                .where(FileRecord.campaign_id == campaign_id)
                .options(
                    selectinload(FileRecord.organization),
                    selectinload(FileRecord.dimension_values).selectinload(FileDimensionValue.dimension),
                )
                .order_by(FileRecord.first_received_at)
            )
        ).all()
    )


async def _events(session: AsyncSession, campaign_id: int) -> list[FileEvent]:
    return list(
        (
            await session.scalars(
                select(FileEvent)
                .join(FileEvent.file)
                .where(FileRecord.campaign_id == campaign_id)
                .options(
                    selectinload(FileEvent.file).selectinload(FileRecord.organization),
                    selectinload(FileEvent.file)
                    .selectinload(FileRecord.dimension_values)
                    .selectinload(FileDimensionValue.dimension),
                )
                .order_by(FileEvent.occurred_at)
            )
        ).all()
    )


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo:
        return value
    return value.replace(tzinfo=UTC)


def _matches_dimensions(file: FileRecord, expected: dict[str, str]) -> bool:
    actual = file_dimensions(file)
    return all(actual.get(key, "").casefold() == value.casefold() for key, value in expected.items() if value)


def _filter_files(files: list[FileRecord], filters: dict[str, Any]) -> list[FileRecord]:
    result = []
    expected_dimensions = filters.get("dimensions") or {}
    for file in files:
        if "organization_ids" in filters and file.organization_id not in filters["organization_ids"]:
            continue
        if filters.get("organization_id") and file.organization_id != filters["organization_id"]:
            continue
        parse_status = filters.get("parse_status")
        if parse_status and file.parse_status.value != str(getattr(parse_status, "value", parse_status)):
            continue
        if filters.get("deleted") is not None and file.deleted is not filters["deleted"]:
            continue
        if filters.get("updated_only") and file.revision_count == 0:
            continue
        if filters.get("attention_only") and (file.parse_status == ParseStatus.OK or file.warning_ignored):
            continue
        extension = str(filters.get("extension") or "").strip().lstrip(".").casefold()
        if extension and not file.filename.casefold().endswith(f".{extension}"):
            continue
        search = str(filters.get("search") or "").strip().casefold()
        organization_text = ""
        if file.organization:
            organization_text = " ".join(
                [file.organization.code, file.organization.name, file.organization.short_name]
            ).casefold()
        if search and search not in f"{file.filename} {file.full_path}".casefold() and search not in organization_text:
            continue
        if filters.get("date_from") and _aware(file.first_received_at) < _aware(filters["date_from"]):
            continue
        if filters.get("date_to") and _aware(file.first_received_at) > _aware(filters["date_to"]):
            continue
        if expected_dimensions and not _matches_dimensions(file, expected_dimensions):
            continue
        result.append(file)
    return result


def _filter_events(events: list[FileEvent], filters: dict[str, Any]) -> list[FileEvent]:
    result = []
    for event in events:
        if filters.get("organization_id") and event.file.organization_id != filters["organization_id"]:
            continue
        event_type = filters.get("event_type")
        if event_type and event.event_type.value != str(getattr(event_type, "value", event_type)):
            continue
        search = str(filters.get("search") or "").strip().casefold()
        if search and search not in f"{event.file.filename} {event.new_path} {event.old_path}".casefold():
            continue
        if filters.get("date_from") and _aware(event.occurred_at) < _aware(filters["date_from"]):
            continue
        if filters.get("date_to") and _aware(event.occurred_at) > _aware(filters["date_to"]):
            continue
        if filters.get("dimensions") and not _matches_dimensions(event.file, filters["dimensions"]):
            continue
        result.append(event)
    return result


async def build_xlsx(
    session: AsyncSession, campaign_id: int, filters: dict[str, Any] | None = None
) -> tuple[Path, str]:
    campaign = await session.scalar(
        select(Campaign).where(Campaign.id == campaign_id).options(selectinload(Campaign.dimensions))
    )
    if campaign is None:
        raise ValueError("Кампания не найдена")
    filters = filters or {}
    progress = filter_progress(
        await build_progress(session, campaign_id),
        status_filter=filters.get("status", ""),
        search=filters.get("search", ""),
        organization_id=filters.get("organization_id"),
        dimensions=filters.get("dimensions"),
    )
    files = await _files(session, campaign_id)
    events = await _events(session, campaign_id)
    if any(filters.get(key) for key in ("status", "search", "organization_id", "dimensions")):
        selected_organizations = {item["organization"]["id"] for item in progress["organizations"]}
        file_filters = {
            "organization_ids": selected_organizations,
            "dimensions": filters.get("dimensions") or {},
        }
        files = _filter_files(files, file_filters)
        selected_file_ids = {file.id for file in files}
        events = [event for event in events if event.file_id in selected_file_ids]
    workbook = Workbook(write_only=True)

    summary = workbook.create_sheet("Сводка")
    summary.append(["Кампания", campaign.name])
    summary.append(["Период", campaign.start_at, campaign.deadline_at])
    summary.append(["Дата формирования", _date_cell(summary, datetime.now(UTC))])
    summary.append(["Организаций", len(progress["organizations"])])
    summary.append(["Полностью сдали", progress["counts"]["submitted"]])
    summary.append(["Частично", progress["counts"]["partial"]])
    summary.append(["Не сдали", progress["counts"]["not_submitted"]])
    summary.append(["Выполнение, %", progress["percent"]])
    summary.append(["Файлов", len(files)])
    summary.append([])
    summary_headers = ["Организация", "Требуется", "Сдано", "Выполнение, %", "Последнее поступление", "Статус"]
    summary.append(_header_cells(summary, summary_headers))
    for item in progress["organizations"]:
        summary.append(
            [
                item["organization"]["name"],
                item["required"],
                item["completed"],
                item["percent"],
                _date_cell(summary, item["last_received_at"]),
                STATUS_LABELS.get(item["status"], item["status"]),
            ]
        )
    for index, width in enumerate([38, 12, 12, 16, 22, 18], start=1):
        summary.column_dimensions[get_column_letter(index)].width = width
    summary.freeze_panes = "A11"
    if progress["organizations"]:
        summary.auto_filter.ref = f"A11:F{11 + len(progress['organizations'])}"

    completion = workbook.create_sheet("Выполнение")
    completion_headers = [
        "Организация",
        *[dimension.name for dimension in campaign.dimensions],
        "Требуется",
        "Получено",
        "Первая сдача",
        "Последняя сдача",
        "Статус",
    ]
    completion.append(_header_cells(completion, completion_headers))
    completion_rows = 0
    for org in progress["organizations"]:
        for requirement in org["requirements"]:
            dims = requirement["dimensions"]
            dim_values = [dims.get(dimension.key, "") for dimension in campaign.dimensions]
            completion.append(
                [
                    org["organization"]["name"],
                    *dim_values,
                    requirement["required"],
                    requirement["received"],
                    _date_cell(completion, requirement["first_received_at"]),
                    _date_cell(completion, requirement["last_received_at"]),
                    STATUS_LABELS.get(requirement["status"], requirement["status"]),
                ]
            )
            completion_rows += 1
    _configure_table(completion, [38] + [22] * (len(completion_headers) - 1), completion_rows, len(completion_headers))

    missing_sheet = workbook.create_sheet("Не сдали")
    missing_headers = ["Организация", "Параметры", "Срок", "Статус"]
    missing_sheet.append(_header_cells(missing_sheet, missing_headers))
    missing_rows = 0
    for org in progress["organizations"]:
        for requirement in org["requirements"]:
            if requirement["status"] in {"submitted", "overdue"}:
                continue
            params = ", ".join(requirement["dimensions"].values())
            missing_sheet.append(
                [
                    org["organization"]["name"],
                    params,
                    _date_cell(missing_sheet, requirement["deadline_at"]),
                    STATUS_LABELS.get(requirement["status"], requirement["status"]),
                ]
            )
            missing_rows += 1
    _configure_table(missing_sheet, [38, 45, 22, 18], missing_rows, len(missing_headers))

    file_sheet = workbook.create_sheet("Файлы")
    dimension_keys = [dimension.key for dimension in campaign.dimensions]
    dimension_names = [dimension.name for dimension in campaign.dimensions]
    file_headers = [
        "Организация",
        *dimension_names,
        "Файл",
        "Тип",
        "Размер",
        "Поступил",
        "Обновлён",
        "Обновлений",
        "Путь",
        "Состояние",
    ]
    file_sheet.append(_header_cells(file_sheet, file_headers))
    for file in files:
        dimensions = file_dimensions(file)
        if file.deleted:
            state = "Удалено"
        elif file.parse_status != ParseStatus.OK:
            state = "Не распознано"
        elif file.revision_count:
            state = "Обновлено"
        else:
            state = "Получено"
        file_sheet.append(
            [
                file.organization.name if file.organization else "Не распознано",
                *[dimensions.get(key, "") for key in dimension_keys],
                file.filename,
                file.mime_type,
                file.size,
                _date_cell(file_sheet, file.first_received_at),
                _date_cell(file_sheet, file.last_received_at),
                file.revision_count,
                file.full_path,
                state,
            ]
        )
    _configure_table(
        file_sheet, [32] + [20] * len(dimension_keys) + [34, 22, 14, 22, 22, 12, 60, 18], len(files), len(file_headers)
    )

    event_sheet = workbook.create_sheet("Активность")
    event_headers = ["Время", "Организация", "Действие", "Файл", "Путь", "Пользователь"]
    event_sheet.append(_header_cells(event_sheet, event_headers))
    for event in events:
        event_sheet.append(
            [
                _date_cell(event_sheet, event.occurred_at),
                event.file.organization.name if event.file.organization else "Не распознано",
                EVENT_LABELS.get(event.event_type.value, event.event_type.value),
                event.file.filename,
                event.new_path or event.old_path,
                event.actor_user_id,
            ]
        )
    _configure_table(event_sheet, [22, 36, 28, 35, 60, 24], len(events), len(event_headers))

    errors_sheet = workbook.create_sheet("Ошибки")
    error_headers = ["Организация", "Файл", "Путь", "Проблема", "Игнорировано"]
    errors_sheet.append(_header_cells(errors_sheet, error_headers))
    errors = [file for file in files if file.parse_status != ParseStatus.OK]
    for file in errors:
        errors_sheet.append(
            [
                file.organization.name if file.organization else "Не распознано",
                file.filename,
                file.full_path,
                file.parse_message,
                "Да" if file.warning_ignored else "Нет",
            ]
        )
    _configure_table(errors_sheet, [36, 34, 60, 60, 14], len(errors), len(error_headers))

    output = _temp_report(".xlsx")
    workbook.save(output)
    filename = safe_filename(f"Отчёт_{campaign.name}_{datetime.now().date().isoformat()}") + ".xlsx"
    return output, filename


def _csv_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).strftime("%d.%m.%Y %H:%M") if value.tzinfo else value.strftime("%d.%m.%Y %H:%M")
    return value


async def build_csv(
    session: AsyncSession,
    campaign_id: int,
    table: str,
    filters: dict[str, Any] | None = None,
) -> tuple[Path, str]:
    campaign = await session.get(Campaign, campaign_id)
    if campaign is None:
        raise ValueError("Кампания не найдена")
    filters = filters or {}
    output = _temp_report(".csv")
    with output.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        if table == "progress":
            progress = filter_progress(
                await build_progress(session, campaign_id),
                status_filter=filters.get("status", ""),
                search=filters.get("search", ""),
                organization_id=filters.get("organization_id"),
                dimensions=filters.get("dimensions"),
            )
            writer.writerow(["Организация", "Выполнено", "Требуется", "Выполнение, %", "Последняя сдача", "Статус"])
            for item in progress["organizations"]:
                writer.writerow(
                    [
                        item["organization"]["name"],
                        item["completed"],
                        item["required"],
                        item["percent"],
                        _csv_value(item["last_received_at"]),
                        STATUS_LABELS.get(item["status"], item["status"]),
                    ]
                )
        elif table == "events":
            events = _filter_events(await _events(session, campaign_id), filters)
            writer.writerow(["Время", "Организация", "Действие", "Файл", "Путь"])
            for event in events:
                writer.writerow(
                    [
                        _csv_value(event.occurred_at),
                        event.file.organization.name if event.file.organization else "Не распознано",
                        EVENT_LABELS.get(event.event_type.value, event.event_type.value),
                        event.file.filename,
                        event.new_path or event.old_path,
                    ]
                )
        else:
            files = _filter_files(await _files(session, campaign_id), filters)
            writer.writerow(["Организация", "Параметры", "Файл", "Размер", "Поступил", "Обновлён", "Путь", "Состояние"])
            for file in files:
                writer.writerow(
                    [
                        file.organization.name if file.organization else "Не распознано",
                        ", ".join(file_dimensions(file).values()),
                        file.filename,
                        file.size,
                        _csv_value(file.first_received_at),
                        _csv_value(file.last_received_at),
                        file.full_path,
                        "Удалено"
                        if file.deleted
                        else ("Не распознано" if file.parse_status != ParseStatus.OK else "Получено"),
                    ]
                )
    filename = safe_filename(f"{table}_{campaign.name}_{datetime.now().date().isoformat()}") + ".csv"
    return output, filename


def remove_report(path: Path) -> None:
    with suppress(FileNotFoundError):
        os.unlink(path)
