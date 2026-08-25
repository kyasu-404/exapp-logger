from __future__ import annotations

import csv
import io
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openpyxl import load_workbook
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..models import (
    Campaign,
    CampaignOrganization,
    Dimension,
    DimensionValue,
    Organization,
    Requirement,
)


def normalize_header(value: Any) -> str:
    text = str(value or "").strip().casefold().replace("ё", "е")
    return re.sub(r"[\s_\-/]+", " ", text)


def _decode_csv(content: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("CSV должен быть в кодировке UTF-8 или Windows-1251")


def read_tabular(filename: str, content: bytes) -> list[dict[str, Any]]:
    suffix = Path(filename).suffix.casefold()
    if suffix == ".csv":
        text = _decode_csv(content)
        sample = text[:4096]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=";,\t,")
            delimiter = dialect.delimiter
        except csv.Error:
            delimiter = ";"
        reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
        return [
            {str(key or "").strip(): value.strip() if isinstance(value, str) else value for key, value in row.items()}
            for row in reader
            if any(value not in (None, "") for value in row.values())
        ]
    if suffix in {".xlsx", ".xlsm"}:
        workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        sheet = workbook.active
        iterator = sheet.iter_rows(values_only=True)
        headers = [str(value or "").strip() for value in next(iterator, ())]
        rows = []
        for values in iterator:
            row = {
                headers[index]: value for index, value in enumerate(values) if index < len(headers) and headers[index]
            }
            if any(value not in (None, "") for value in row.values()):
                rows.append(row)
        workbook.close()
        return rows
    raise ValueError("Поддерживаются только файлы XLSX и CSV")


def remap(row: dict[str, Any]) -> dict[str, Any]:
    return {normalize_header(key): value for key, value in row.items()}


def pick(row: dict[str, Any], *aliases: str, default: Any = "") -> Any:
    normalized = remap(row)
    for alias in aliases:
        if normalize_header(alias) in normalized and normalized[normalize_header(alias)] not in (None, ""):
            return normalized[normalize_header(alias)]
    return default


def parse_bool(value: Any, default: bool = True) -> bool:
    if value in (None, ""):
        return default
    return str(value).strip().casefold() in {"1", "да", "yes", "true", "обязательно", "+"}


def parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text = str(value).strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d.%m.%Y %H:%M", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    raise ValueError(f"Некорректная дата: {text}")


async def import_organizations(session: AsyncSession, rows: list[dict[str, Any]], commit: bool) -> dict[str, Any]:
    existing = list((await session.scalars(select(Organization))).all())
    by_code = {item.code.casefold(): item for item in existing}
    by_folder = {item.folder_name.casefold(): item for item in existing}
    errors: list[dict[str, Any]] = []
    preview: list[dict[str, Any]] = []
    to_add = to_update = 0
    for index, raw in enumerate(rows, start=2):
        try:
            code = str(pick(raw, "Код", "Code")).strip()
            name = str(pick(raw, "Название", "Организация", "Name")).strip()
            short_name = str(pick(raw, "Краткое название", "Short name")).strip()
            folder = str(pick(raw, "Имя папки", "Папка", "Folder", default=code)).strip()
            email = str(pick(raw, "Email", "Эл. почта")).strip()
            if not code or not name or not folder:
                raise ValueError("обязательны Код, Название и Имя папки")
            item = by_code.get(code.casefold()) or by_folder.get(folder.casefold())
            action = "update" if item else "add"
            to_update += int(item is not None)
            to_add += int(item is None)
            normalized = {
                "code": code,
                "name": name,
                "short_name": short_name,
                "folder_name": folder,
                "email": email,
                "action": action,
            }
            preview.append(normalized)
            if commit:
                if item is None:
                    item = Organization(**{key: value for key, value in normalized.items() if key != "action"})
                    session.add(item)
                    by_code[code.casefold()] = item
                    by_folder[folder.casefold()] = item
                else:
                    for key, value in normalized.items():
                        if key != "action":
                            setattr(item, key, value)
        except (ValueError, TypeError) as exc:
            errors.append({"row": index, "message": str(exc), "data": raw})
    if commit:
        await session.commit()
    return {
        "rows": len(rows),
        "to_add": to_add,
        "to_update": to_update,
        "errors": errors,
        "preview": preview[:100],
        "committed": commit,
    }


async def _campaign_for_import(session: AsyncSession, campaign_id: int) -> Campaign:
    campaign = await session.scalar(
        select(Campaign)
        .where(Campaign.id == campaign_id)
        .options(
            selectinload(Campaign.dimensions).selectinload(Dimension.values),
            selectinload(Campaign.organizations).selectinload(CampaignOrganization.organization),
        )
    )
    if campaign is None:
        raise ValueError("Кампания не найдена")
    return campaign


def requirement_identity(dimensions: dict[str, str]) -> str:
    return json.dumps(dimensions, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


async def import_requirements(
    session: AsyncSession, campaign_id: int, rows: list[dict[str, Any]], commit: bool
) -> dict[str, Any]:
    campaign = await _campaign_for_import(session, campaign_id)
    links = [link for link in campaign.organizations if link.included]
    organizations = [link.organization for link in links]
    existing_requirements = list(
        (await session.scalars(select(Requirement).where(Requirement.campaign_id == campaign_id))).all()
    )
    by_identity = {(item.organization_id, item.identity_key): item for item in existing_requirements}
    errors: list[dict[str, Any]] = []
    preview: list[dict[str, Any]] = []
    to_add = to_update = 0
    for index, raw in enumerate(rows, start=2):
        try:
            org_key = str(pick(raw, "Организация", "Код", "Школа", "Organization")).strip().casefold()
            organization = next(
                (
                    item
                    for item in organizations
                    if org_key
                    in {
                        item.code.casefold(),
                        item.folder_name.casefold(),
                        item.name.casefold(),
                        item.short_name.casefold(),
                    }
                ),
                None,
            )
            if organization is None:
                raise ValueError(f"организация «{org_key or 'не указана'}» не участвует в кампании")
            dimensions: dict[str, str] = {}
            normalized_raw = remap(raw)
            for dimension in campaign.dimensions:
                value = normalized_raw.get(
                    normalize_header(dimension.name), normalized_raw.get(normalize_header(dimension.key), "")
                )
                value = str(value or "").strip()
                if dimension.required and not value:
                    raise ValueError(f"не заполнено поле «{dimension.name}»")
                if value:
                    if dimension.fixed and not any(
                        candidate.active and candidate.value.casefold() == value.casefold()
                        for candidate in dimension.values
                    ):
                        raise ValueError(f"неизвестное значение «{value}» для «{dimension.name}»")
                    dimensions[dimension.key] = value
            required = parse_bool(pick(raw, "Обязательно", "Required", default="Да"))
            count = int(pick(raw, "Количество файлов", "Количество", "File count", default=1))
            if count < 1:
                raise ValueError("количество файлов должно быть не меньше 1")
            deadline = parse_datetime(pick(raw, "Срок", "Deadline", default=""))
            identity = requirement_identity(dimensions)
            item = by_identity.get((organization.id, identity))
            action = "update" if item else "add"
            to_update += int(item is not None)
            to_add += int(item is None)
            normalized = {
                "organization_id": organization.id,
                "organization": organization.short_name or organization.name,
                "dimensions": dimensions,
                "required": required,
                "required_file_count": count,
                "deadline_at": deadline,
                "action": action,
            }
            preview.append(normalized)
            if commit:
                if item is None:
                    item = Requirement(
                        campaign_id=campaign_id,
                        organization_id=organization.id,
                        identity_key=identity,
                        dimensions=dimensions,
                    )
                    session.add(item)
                    by_identity[(organization.id, identity)] = item
                item.required = required
                item.required_file_count = count
                item.deadline_at = deadline
        except (ValueError, TypeError) as exc:
            errors.append({"row": index, "message": str(exc), "data": raw})
    if commit:
        campaign.requirements_mode = "matrix"
        await session.commit()
    return {
        "rows": len(rows),
        "to_add": to_add,
        "to_update": to_update,
        "errors": errors,
        "preview": preview[:100],
        "committed": commit,
    }


async def import_dimension_values(
    session: AsyncSession, campaign_id: int, rows: list[dict[str, Any]], commit: bool
) -> dict[str, Any]:
    campaign = await _campaign_for_import(session, campaign_id)
    by_header = {
        normalize_header(alias): dimension
        for dimension in campaign.dimensions
        for alias in (dimension.key, dimension.name)
    }
    errors: list[dict[str, Any]] = []
    preview: list[dict[str, Any]] = []
    existing = {
        (dimension.id, value.value.casefold()): value for dimension in campaign.dimensions for value in dimension.values
    }
    to_add = to_update = 0
    seen: set[tuple[int, str]] = set()
    for row_index, raw in enumerate(rows, start=2):
        for header, raw_value in raw.items():
            if raw_value in (None, ""):
                continue
            dimension = by_header.get(normalize_header(header))
            if dimension is None:
                errors.append({"row": row_index, "message": f"неизвестный уровень «{header}»", "data": raw})
                continue
            value = str(raw_value).strip()
            key = (dimension.id, value.casefold())
            if key in seen:
                continue
            seen.add(key)
            item = existing.get(key)
            action = "update" if item else "add"
            to_update += int(item is not None)
            to_add += int(item is None)
            preview.append({"dimension": dimension.name, "value": value, "action": action})
            if commit:
                if item is None:
                    item = DimensionValue(
                        dimension_id=dimension.id,
                        value=value,
                        display_name=value,
                        sort_order=len(dimension.values),
                    )
                    session.add(item)
                    existing[key] = item
                else:
                    item.display_name = value
                    item.active = True
    if commit:
        await session.commit()
    return {
        "rows": len(rows),
        "to_add": to_add,
        "to_update": to_update,
        "errors": errors,
        "preview": preview[:100],
        "committed": commit,
    }
