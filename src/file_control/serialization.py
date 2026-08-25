from __future__ import annotations

from typing import Any

from .models import Campaign, Dimension, FileEvent, FileRecord, Organization, Requirement


def organization_dict(item: Organization) -> dict[str, Any]:
    return {
        "id": item.id,
        "code": item.code,
        "name": item.name,
        "short_name": item.short_name,
        "folder_name": item.folder_name,
        "email": item.email,
        "active": item.active,
        "extra_fields": item.extra_fields or {},
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def dimension_dict(item: Dimension) -> dict[str, Any]:
    return {
        "id": item.id,
        "position": item.position,
        "key": item.key,
        "name": item.name,
        "required": item.required,
        "fixed": item.fixed,
        "values": [
            {
                "id": value.id,
                "value": value.value,
                "display_name": value.display_name,
                "sort_order": value.sort_order,
                "active": value.active,
            }
            for value in item.values
        ],
    }


def campaign_dict(item: Campaign, *, include_config: bool = False) -> dict[str, Any]:
    result = {
        "id": item.id,
        "name": item.name,
        "campaign_type": item.campaign_type,
        "year": item.year,
        "description": item.description,
        "root_path": item.root_path,
        "storage_user_id": item.storage_user_id,
        "start_at": item.start_at,
        "deadline_at": item.deadline_at,
        "status": item.status.value,
        "structure_mode": item.structure_mode.value,
        "requirements_mode": item.requirements_mode,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
        "created_by": item.created_by,
    }
    if include_config:
        result.update(
            {
                "settings": item.settings or {},
                "organization_ids": [link.organization_id for link in item.organizations if link.included],
                "dimensions": [dimension_dict(dimension) for dimension in item.dimensions],
            }
        )
    return result


def file_dimensions(item: FileRecord) -> dict[str, str]:
    return {link.dimension.key: link.raw_value for link in item.dimension_values}


def file_dict(item: FileRecord) -> dict[str, Any]:
    return {
        "id": item.id,
        "campaign_id": item.campaign_id,
        "organization_id": item.organization_id,
        "organization": organization_dict(item.organization) if item.organization else None,
        "filename": item.filename,
        "directory": item.directory,
        "relative_path": item.relative_path,
        "full_path": item.full_path,
        "mime_type": item.mime_type,
        "size": item.size,
        "first_received_at": item.first_received_at,
        "last_received_at": item.last_received_at,
        "node_mtime": item.node_mtime,
        "revision_count": item.revision_count,
        "deleted": item.deleted,
        "deleted_at": item.deleted_at,
        "parse_status": item.parse_status.value,
        "parse_message": item.parse_message,
        "warning_ignored": item.warning_ignored,
        "dimensions": file_dimensions(item),
    }


EVENT_LABELS = {
    "created": "Файл загружен",
    "written": "Файл обновлён",
    "renamed": "Файл перемещён",
    "deleted": "Файл удалён",
    "copied": "Файл скопирован",
    "reconciled_created": "Файл найден при сверке",
    "reconciled_updated": "Изменение найдено при сверке",
    "reconciled_missing": "Файл отсутствует при сверке",
    "reclassified": "Структура файла пересчитана",
}


def event_dict(item: FileEvent) -> dict[str, Any]:
    return {
        "id": item.id,
        "file_id": item.file_id,
        "event_type": item.event_type.value,
        "event_label": EVENT_LABELS[item.event_type.value],
        "occurred_at": item.occurred_at,
        "old_path": item.old_path,
        "new_path": item.new_path,
        "size": item.size,
        "actor_user_id": item.actor_user_id,
        "file": {
            "filename": item.file.filename,
            "organization": item.file.organization.short_name or item.file.organization.name
            if item.file.organization
            else "Не распознано",
            "dimensions": file_dimensions(item.file),
        },
    }


def requirement_dict(item: Requirement, received_count: int = 0, status: str | None = None) -> dict[str, Any]:
    return {
        "id": item.id,
        "campaign_id": item.campaign_id,
        "organization_id": item.organization_id,
        "organization": organization_dict(item.organization),
        "dimensions": item.dimensions or {},
        "required": item.required,
        "required_file_count": item.required_file_count,
        "received_count": received_count,
        "deadline_at": item.deadline_at,
        "status": status,
    }
