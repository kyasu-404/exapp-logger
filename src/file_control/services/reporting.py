from __future__ import annotations

import csv
import os
import re
import uuid
from collections.abc import Iterable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..models import ActivityEvent, EventType, FileRecord, FolderRecord, Watch
from ..serialization import EVENT_LABELS, file_state
from ..time_utils import as_local_naive

STATUS_LABELS = {"new": "Есть", "updated": "Обновлён", "deleted": "Удалён"}


def human_size(value: int | None) -> str:
    size = max(0, int(value or 0))
    if size < 1024:
        return f"{size} Б"
    units = ("КБ", "МБ", "ГБ", "ТБ")
    result = size / 1024
    unit = units[0]
    for candidate in units[1:]:
        if result < 1024:
            break
        result /= 1024
        unit = candidate
    precision = 0 if result >= 10 else 1
    rendered = f"{result:.{precision}f}".replace(".", ",")
    return f"{rendered} {unit}"


def report_stamp(value: datetime | None = None) -> str:
    moment = value or datetime.now(UTC)
    local = as_local_naive(moment)
    return local.strftime("%Y-%m-%d_%H-%M-%S") if local else "report"


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
    cell = WriteOnlyCell(sheet, value=as_local_naive(value))
    cell.number_format = "dd.mm.yyyy hh:mm"
    return cell


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo:
        return value
    return value.replace(tzinfo=UTC)


def _escaped_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _file_conditions(watch_id: int, filters: dict[str, Any]) -> list[Any]:
    conditions: list[Any] = [FileRecord.watch_id == watch_id]
    search = str(filters.get("search") or "").strip()
    if search:
        token = f"%{_escaped_like(search)}%"
        conditions.append(
            or_(
                FileRecord.filename.ilike(token, escape="\\"),
                FileRecord.full_path.ilike(token, escape="\\"),
            )
        )
    extension = str(filters.get("extension") or "").strip().lstrip(".").lower()
    if extension:
        conditions.append(FileRecord.extension == extension)
    status = str(filters.get("status") or "")
    if status == "deleted":
        conditions.append(FileRecord.exists.is_(False))
    elif status == "updated":
        conditions.extend((FileRecord.exists.is_(True), FileRecord.revision_count > 0))
    elif status == "new":
        conditions.extend((FileRecord.exists.is_(True), FileRecord.revision_count == 0))
    prefix = str(filters.get("path_prefix") or "").strip().strip("/")
    if prefix:
        escaped = _escaped_like(prefix)
        conditions.append(
            or_(
                FileRecord.relative_path.ilike(escaped, escape="\\"),
                FileRecord.relative_path.ilike(f"{escaped}/%", escape="\\"),
            )
        )
    if filters.get("date_from"):
        conditions.append(FileRecord.first_seen_at >= filters["date_from"])
    if filters.get("date_to"):
        conditions.append(FileRecord.first_seen_at <= filters["date_to"])
    return conditions


def _event_conditions(watch: Watch, filters: dict[str, Any]) -> list[Any]:
    conditions: list[Any] = [ActivityEvent.watch_id == watch.id]
    search = str(filters.get("search") or "").strip()
    if search:
        token = f"%{_escaped_like(search)}%"
        conditions.append(
            or_(
                ActivityEvent.path.ilike(token, escape="\\"),
                ActivityEvent.old_path.ilike(token, escape="\\"),
            )
        )
    event_type = filters.get("event_type")
    if event_type:
        conditions.append(ActivityEvent.event_type == event_type)
    prefix = str(filters.get("path_prefix") or "").strip().strip("/")
    if prefix:
        full_prefix = f"{watch.root_path.rstrip('/')}/{prefix}"
        escaped = _escaped_like(full_prefix)
        conditions.append(
            or_(
                ActivityEvent.path.ilike(escaped, escape="\\"),
                ActivityEvent.path.ilike(f"{escaped}/%", escape="\\"),
                ActivityEvent.old_path.ilike(escaped, escape="\\"),
                ActivityEvent.old_path.ilike(f"{escaped}/%", escape="\\"),
            )
        )
    if filters.get("date_from"):
        conditions.append(ActivityEvent.event_time >= filters["date_from"])
    if filters.get("date_to"):
        conditions.append(ActivityEvent.event_time <= filters["date_to"])
    return conditions


def _folder_conditions(watch_id: int, filters: dict[str, Any]) -> list[Any]:
    conditions: list[Any] = [FolderRecord.watch_id == watch_id]
    prefix = str(filters.get("path_prefix") or "").strip().strip("/")
    if prefix:
        escaped = _escaped_like(prefix)
        conditions.append(
            or_(
                FolderRecord.relative_path.ilike(escaped, escape="\\"),
                FolderRecord.relative_path.ilike(f"{escaped}/%", escape="\\"),
            )
        )
    if filters.get("date_from"):
        conditions.append(FolderRecord.first_seen_at >= filters["date_from"])
    if filters.get("date_to"):
        conditions.append(FolderRecord.first_seen_at <= filters["date_to"])
    return conditions


def _ensure_folder_row(rows: dict[str, dict[str, Any]], path: str) -> dict[str, Any]:
    return rows.setdefault(
        path,
        {"path": path, "files": 0, "first_seen_at": None, "last_seen_at": None, "size": 0},
    )


def _accumulate_folder_record(rows: dict[str, dict[str, Any]], folder: FolderRecord) -> None:
    if not folder.exists:
        return
    parts = [part for part in PurePosixPath(folder.relative_path).parts if part not in {"/", "", "."}]
    for depth in range(1, len(parts) + 1):
        row = _ensure_folder_row(rows, "/".join(parts[:depth]))
        if row["first_seen_at"] is None or folder.first_seen_at < row["first_seen_at"]:
            row["first_seen_at"] = folder.first_seen_at
        if row["last_seen_at"] is None or folder.last_seen_at > row["last_seen_at"]:
            row["last_seen_at"] = folder.last_seen_at


def _accumulate_file(rows: dict[str, dict[str, Any]], file: FileRecord) -> None:
    parts = [part for part in PurePosixPath(file.parent_path).parts if part not in {"/", "", "."}]
    for depth in range(1, len(parts) + 1):
        row = _ensure_folder_row(rows, "/".join(parts[:depth]))
        if file.exists:
            row["files"] += 1
            row["size"] += file.size
        if row["first_seen_at"] is None or file.first_seen_at < row["first_seen_at"]:
            row["first_seen_at"] = file.first_seen_at
        if row["last_seen_at"] is None or file.last_seen_at > row["last_seen_at"]:
            row["last_seen_at"] = file.last_seen_at


async def build_xlsx(session: AsyncSession, watch_id: int, filters: dict[str, Any] | None = None) -> tuple[Path, str]:
    watch = await session.get(Watch, watch_id)
    if watch is None:
        raise ValueError("Наблюдение не найдено")
    filters = filters or {}
    generated_at = datetime.now(UTC)
    file_conditions = _file_conditions(watch_id, filters)
    event_conditions = _event_conditions(watch, filters)
    file_count = await session.scalar(select(func.count(FileRecord.id)).where(*file_conditions)) or 0
    event_count = await session.scalar(select(func.count(ActivityEvent.id)).where(*event_conditions)) or 0
    max_depth = 0
    parent_paths = await session.stream_scalars(
        select(FileRecord.parent_path).where(*file_conditions).execution_options(yield_per=500)
    )
    async for parent_path in parent_paths:
        max_depth = max(
            max_depth,
            len([part for part in PurePosixPath(parent_path).parts if part not in {"/", "", "."}]),
        )
    folder_data: dict[str, dict[str, Any]] = {}
    folder_stream = await session.stream_scalars(
        select(FolderRecord)
        .where(*_folder_conditions(watch_id, filters))
        .order_by(FolderRecord.relative_path, FolderRecord.id)
        .execution_options(yield_per=500)
    )
    async for folder_record in folder_stream:
        _accumulate_folder_record(folder_data, folder_record)
    workbook = Workbook(write_only=True)

    file_sheet = workbook.create_sheet("Файлы")
    folder_headers = [f"Папка {index}" for index in range(1, max_depth + 1)]
    file_headers = [
        *folder_headers,
        "Полный путь",
        "Файл",
        "Поступил",
        "Последнее событие",
        "Время загрузки в Nextcloud",
        "Дата файла (mtime)",
        "Размер",
        "Тип",
        "Статус",
    ]
    file_sheet.append(_header_cells(file_sheet, file_headers))
    file_stream = await session.stream_scalars(
        select(FileRecord)
        .where(*file_conditions)
        .order_by(FileRecord.first_seen_at, FileRecord.id)
        .execution_options(yield_per=500)
    )
    async for file in file_stream:
        parts = [part for part in PurePosixPath(file.parent_path).parts if part not in {"/", "", "."}]
        file_sheet.append(
            [
                *parts,
                *([None] * (max_depth - len(parts))),
                file.full_path,
                file.filename,
                _date_cell(file_sheet, file.first_seen_at),
                _date_cell(file_sheet, file.last_seen_at),
                _date_cell(file_sheet, file.nextcloud_upload_time),
                _date_cell(file_sheet, file.nextcloud_mtime),
                human_size(file.size),
                file.mime_type,
                STATUS_LABELS[file_state(file)],
            ]
        )
        _accumulate_file(folder_data, file)
    _configure_table(
        file_sheet,
        [22] * max_depth + [65, 35, 22, 22, 24, 22, 14, 28, 16],
        file_count,
        len(file_headers),
    )

    event_sheet = workbook.create_sheet("Активность")
    event_headers = [
        "Время",
        "Действие",
        "Объект",
        "Полный путь",
        "Старый путь",
        "Имя",
        "Размер",
        "Пользователь",
    ]
    event_sheet.append(_header_cells(event_sheet, event_headers))
    event_stream = await session.stream_scalars(
        select(ActivityEvent)
        .where(*event_conditions)
        .order_by(ActivityEvent.event_time, ActivityEvent.id)
        .execution_options(yield_per=500)
    )
    async for event in event_stream:
        event_sheet.append(
            [
                _date_cell(event_sheet, event.event_time),
                EVENT_LABELS[event.event_type.value],
                "Папка" if event.node_type == "folder" else "Файл",
                event.path or event.new_path,
                event.old_path,
                PurePosixPath(event.path or event.new_path or event.old_path).name,
                human_size(event.size),
                event.actor_user_id,
            ]
        )
    _configure_table(event_sheet, [22, 18, 14, 65, 65, 35, 14, 24], event_count, len(event_headers))

    folder_sheet = workbook.create_sheet("По папкам")
    folder_headers = [
        "Путь папки",
        "Файлов",
        "Первое поступление",
        "Последнее поступление",
        "Размер",
    ]
    folder_sheet.append(_header_cells(folder_sheet, folder_headers))
    folders = sorted(folder_data.values(), key=lambda row: row["path"].casefold())
    for folder in folders:
        folder_sheet.append(
            [
                folder["path"],
                folder["files"],
                _date_cell(folder_sheet, folder["first_seen_at"]),
                _date_cell(folder_sheet, folder["last_seen_at"]),
                human_size(folder["size"]),
            ]
        )
    _configure_table(folder_sheet, [65, 14, 22, 22, 14], len(folders), len(folder_headers))

    notes_sheet = workbook.create_sheet("Пояснения")
    notes_sheet.append(_header_cells(notes_sheet, ["Поле", "Что означает"]))
    notes = [
        ("Отчёт сформирован", _date_cell(notes_sheet, generated_at)),
        ("Поступил", "Когда ExApp впервые зарегистрировал файл."),
        ("Последнее событие", "Когда ExApp в последний раз зафиксировал изменение состояния файла."),
        ("Время загрузки в Nextcloud", "Когда Nextcloud принял файл; значение свойства nc:upload_time."),
        (
            "Дата файла (mtime)",
            "Дата изменения, переданная клиентом или источником файла. Она может быть раньше времени загрузки "
            "и сама по себе не означает редактирование в офисном приложении.",
        ),
        ("Размер", "Размер файла в удобном для чтения виде: Б, КБ, МБ, ГБ или ТБ."),
        (
            "Листы",
            "«Файлы» — текущее состояние файлов; события папок и полная хронология находятся на листе «Активность».",
        ),
    ]
    for row in notes:
        notes_sheet.append(row)
    _configure_table(notes_sheet, [32, 110], len(notes), 2)

    output = _temp_report(".xlsx")
    workbook.save(output)
    filename = safe_filename(f"Наблюдение_{watch.name}_{report_stamp(generated_at)}") + ".xlsx"
    return output, filename


def _csv_value(value: Any) -> Any:
    if isinstance(value, datetime):
        local_value = as_local_naive(value)
        return local_value.strftime("%d.%m.%Y %H:%M") if local_value else ""
    return value


async def build_csv(
    session: AsyncSession,
    watch_id: int,
    table: str,
    filters: dict[str, Any] | None = None,
) -> tuple[Path, str]:
    watch = await session.get(Watch, watch_id)
    if watch is None:
        raise ValueError("Наблюдение не найдено")
    filters = filters or {}
    output = _temp_report(".csv")
    with output.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        if table == "events":
            writer.writerow(
                [
                    "Время",
                    "Действие",
                    "Объект",
                    "Полный путь",
                    "Старый путь",
                    "Имя",
                    "Размер",
                ]
            )
            events = await session.stream_scalars(
                select(ActivityEvent)
                .where(*_event_conditions(watch, filters))
                .order_by(ActivityEvent.event_time, ActivityEvent.id)
                .execution_options(yield_per=500)
            )
            async for event in events:
                writer.writerow(
                    [
                        _csv_value(event.event_time),
                        EVENT_LABELS[event.event_type.value],
                        "Папка" if event.node_type == "folder" else "Файл",
                        event.path or event.new_path,
                        event.old_path,
                        PurePosixPath(event.path or event.new_path or event.old_path).name,
                        human_size(event.size),
                    ]
                )
        elif table == "folders":
            writer.writerow(["Путь папки", "Файлов", "Первое поступление", "Последнее поступление", "Размер"])
            folder_data: dict[str, dict[str, Any]] = {}
            folders = await session.stream_scalars(
                select(FolderRecord).where(*_folder_conditions(watch_id, filters)).execution_options(yield_per=500)
            )
            async for folder_record in folders:
                _accumulate_folder_record(folder_data, folder_record)
            files = await session.stream_scalars(
                select(FileRecord).where(*_file_conditions(watch_id, filters)).execution_options(yield_per=500)
            )
            async for file in files:
                _accumulate_file(folder_data, file)
            for folder in sorted(folder_data.values(), key=lambda row: row["path"].casefold()):
                writer.writerow(
                    [
                        folder["path"],
                        folder["files"],
                        _csv_value(folder["first_seen_at"]),
                        _csv_value(folder["last_seen_at"]),
                        human_size(folder["size"]),
                    ]
                )
        else:
            writer.writerow(
                [
                    "Путь",
                    "Файл",
                    "Поступил",
                    "Последнее событие",
                    "Время загрузки в Nextcloud",
                    "Дата файла (mtime)",
                    "Размер",
                    "Статус",
                ]
            )
            files = await session.stream_scalars(
                select(FileRecord)
                .where(*_file_conditions(watch_id, filters))
                .order_by(FileRecord.first_seen_at, FileRecord.id)
                .execution_options(yield_per=500)
            )
            async for file in files:
                writer.writerow(
                    [
                        file.full_path,
                        file.filename,
                        _csv_value(file.first_seen_at),
                        _csv_value(file.last_seen_at),
                        _csv_value(file.nextcloud_upload_time),
                        _csv_value(file.nextcloud_mtime),
                        human_size(file.size),
                        STATUS_LABELS[file_state(file)],
                    ]
                )
    filename = safe_filename(f"{table}_{watch.name}_{report_stamp()}") + ".csv"
    return output, filename


CHANGE_CATEGORIES = {
    EventType.CREATED: "Добавлено",
    EventType.COPIED: "Добавлено",
    EventType.UPDATED: "Изменено",
    EventType.MOVED: "Перемещено",
    EventType.RENAMED: "Перемещено",
    EventType.DELETED: "Удалено",
}


async def build_changes_xlsx(
    session: AsyncSession,
    watch_id: int,
    date_from: datetime,
    date_to: datetime,
    path_prefix: str = "",
) -> tuple[Path, str]:
    if _aware(date_to) < _aware(date_from):
        raise ValueError("Дата окончания не может быть раньше даты начала")
    watch = await session.get(Watch, watch_id)
    if watch is None:
        raise ValueError("Наблюдение не найдено")
    event_filters = {"date_from": date_from, "date_to": date_to, "path_prefix": path_prefix}
    conditions = _event_conditions(watch, event_filters)
    counts = {label: 0 for label in ("Добавлено", "Изменено", "Перемещено", "Удалено")}
    grouped = (
        await session.execute(
            select(ActivityEvent.event_type, func.count(ActivityEvent.id))
            .where(*conditions)
            .group_by(ActivityEvent.event_type)
        )
    ).all()
    for event_type, count in grouped:
        counts[CHANGE_CATEGORIES[event_type]] += count
    event_count = sum(counts.values())

    workbook = Workbook(write_only=True)
    summary = workbook.create_sheet("Итоги")
    summary.append(_header_cells(summary, ["Показатель", "Значение"]))
    summary.append(["Наблюдение", watch.name])
    summary.append(["Период с", _date_cell(summary, date_from)])
    summary.append(["Период по", _date_cell(summary, date_to)])
    summary.append(["Ветка", path_prefix or "Всё наблюдение"])
    for label, count in counts.items():
        summary.append([label, count])
    _configure_table(summary, [28, 60], 8, 2)

    changes = workbook.create_sheet("Изменения")
    headers = ["Время (МСК)", "Изменение", "Объект", "Путь", "Предыдущий путь", "Размер", "Пользователь"]
    changes.append(_header_cells(changes, headers))
    events = await session.stream_scalars(
        select(ActivityEvent)
        .where(*conditions)
        .order_by(ActivityEvent.event_time, ActivityEvent.id)
        .execution_options(yield_per=500)
    )
    async for event in events:
        changes.append(
            [
                _date_cell(changes, event.event_time),
                CHANGE_CATEGORIES[event.event_type],
                "Папка" if event.node_type == "folder" else "Файл",
                event.path or event.new_path,
                event.old_path,
                human_size(event.size),
                event.actor_user_id,
            ]
        )
    _configure_table(changes, [22, 18, 14, 70, 70, 14, 24], event_count, len(headers))

    output = _temp_report(".xlsx")
    workbook.save(output)
    filename = safe_filename(f"Изменения_{watch.name}_{report_stamp(date_from)}_{report_stamp(date_to)}") + ".xlsx"
    return output, filename


def remove_report(path: Path) -> None:
    with suppress(FileNotFoundError):
        os.unlink(path)
