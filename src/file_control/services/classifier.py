from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import (
    Campaign,
    DimensionValue,
    FileDimensionValue,
    FileRecord,
    Organization,
    ParseStatus,
    StructureMode,
)


def normalize_path(value: str) -> str:
    cleaned = value.replace("\\", "/").strip()
    while "//" in cleaned:
        cleaned = cleaned.replace("//", "/")
    return "/" + cleaned.strip("/")


def relative_to_campaign(path: str, root_path: str) -> str | None:
    normalized_path = normalize_path(path)
    normalized_root = normalize_path(root_path)
    if normalized_path == normalized_root:
        return ""
    prefix = normalized_root.rstrip("/") + "/"
    if not normalized_path.startswith(prefix):
        return None
    return normalized_path[len(prefix) :]


@dataclass(slots=True)
class DimensionMatch:
    dimension_id: int
    dimension_value_id: int | None
    raw_value: str


@dataclass(slots=True)
class Classification:
    organization: Organization | None
    relative_path: str
    status: ParseStatus
    message: str = ""
    dimensions: list[DimensionMatch] = field(default_factory=list)


def classify(campaign: Campaign, path: str) -> Classification:
    relative = relative_to_campaign(path, campaign.root_path)
    if relative is None:
        return Classification(None, "", ParseStatus.OUTSIDE_STRUCTURE, "Файл находится вне корневой папки кампании")

    parts = [part for part in PurePosixPath(relative).parts if part not in ("/", ".")]
    if not parts:
        return Classification(None, relative, ParseStatus.OUTSIDE_STRUCTURE, "Файл находится в корне кампании")

    folder_name = parts[0]
    organization = next(
        (
            link.organization
            for link in campaign.organizations
            if link.included and link.organization.active and link.organization.folder_name == folder_name
        ),
        None,
    )
    if organization is None:
        return Classification(
            None,
            relative,
            ParseStatus.UNKNOWN_ORGANIZATION,
            f"Неизвестная организация: папка «{folder_name}»",
        )

    directory_parts = parts[1:-1]
    if campaign.structure_mode == StructureMode.FREE:
        return Classification(organization, relative, ParseStatus.OK)

    matches: list[DimensionMatch] = []
    for index, dimension in enumerate(campaign.dimensions):
        if index >= len(directory_parts):
            if dimension.required:
                return Classification(
                    organization,
                    relative,
                    ParseStatus.MISSING_DIMENSION,
                    f"Не удалось определить: {dimension.name}",
                    matches,
                )
            continue
        raw = directory_parts[index]
        matched_value: DimensionValue | None = None
        if dimension.fixed:
            matched_value = next(
                (value for value in dimension.values if value.active and value.value.casefold() == raw.casefold()),
                None,
            )
            if matched_value is None:
                return Classification(
                    organization,
                    relative,
                    ParseStatus.UNKNOWN_DIMENSION,
                    f"Неизвестное значение «{raw}» для уровня «{dimension.name}»",
                    matches + [DimensionMatch(dimension.id, None, raw)],
                )
        matches.append(DimensionMatch(dimension.id, matched_value.id if matched_value else None, raw))

    allow_extra = bool((campaign.settings or {}).get("allow_extra_levels", False))
    if len(directory_parts) > len(campaign.dimensions) and not allow_extra:
        extra = "/".join(directory_parts[len(campaign.dimensions) :])
        return Classification(
            organization,
            relative,
            ParseStatus.OUTSIDE_STRUCTURE,
            f"Лишние уровни папок: {extra}",
            matches,
        )
    return Classification(organization, relative, ParseStatus.OK, dimensions=matches)


async def apply_classification(session: AsyncSession, file: FileRecord, result: Classification) -> None:
    file.organization_id = result.organization.id if result.organization else None
    file.relative_path = result.relative_path
    file.parse_status = result.status
    file.parse_message = result.message
    file.warning_ignored = False
    await session.execute(delete(FileDimensionValue).where(FileDimensionValue.file_id == file.id))
    for match in result.dimensions:
        session.add(
            FileDimensionValue(
                file_id=file.id,
                dimension_id=match.dimension_id,
                dimension_value_id=match.dimension_value_id,
                raw_value=match.raw_value,
            )
        )
