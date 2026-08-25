from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..models import (
    Campaign,
    CampaignOrganization,
    FileDimensionValue,
    FileRecord,
    ParseStatus,
    Requirement,
)


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


async def _campaign_graph(session: AsyncSession, campaign_id: int) -> Campaign | None:
    return await session.scalar(
        select(Campaign)
        .where(Campaign.id == campaign_id)
        .options(
            selectinload(Campaign.organizations).selectinload(CampaignOrganization.organization),
            selectinload(Campaign.dimensions),
            selectinload(Campaign.requirements).selectinload(Requirement.organization),
        )
    )


async def _file_facts(session: AsyncSession, campaign_id: int) -> list[dict[str, Any]]:
    files = (
        await session.scalars(
            select(FileRecord)
            .where(
                FileRecord.campaign_id == campaign_id,
                FileRecord.deleted.is_(False),
                FileRecord.parse_status == ParseStatus.OK,
            )
            .options(selectinload(FileRecord.dimension_values).selectinload(FileDimensionValue.dimension))
        )
    ).all()
    return [
        {
            "id": file.id,
            "organization_id": file.organization_id,
            "first_received_at": file.first_received_at,
            "last_received_at": file.last_received_at,
            "dimensions": {item.dimension.key: item.raw_value for item in file.dimension_values},
        }
        for file in files
    ]


def requirement_status(
    requirement: Requirement,
    matching_files: list[dict[str, Any]],
    campaign_deadline: datetime | None,
) -> str:
    count = len(matching_files)
    if count == 0:
        return "not_submitted"
    if count < requirement.required_file_count:
        return "partial"
    deadline = _aware(requirement.deadline_at or campaign_deadline)
    first = min(_aware(item["first_received_at"]) for item in matching_files)
    if deadline and first and first > deadline:
        return "overdue"
    return "submitted"


def filter_progress(
    progress: dict[str, Any],
    *,
    status_filter: str = "",
    search: str = "",
    organization_id: int | None = None,
    dimensions: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Apply UI/report filters and recalculate totals for the visible requirement set."""
    result = deepcopy(progress)
    dimension_filters = {key: value for key, value in (dimensions or {}).items() if value}
    organizations: list[dict[str, Any]] = []
    needle = search.strip().casefold()
    for original in result["organizations"]:
        item = original
        organization = item["organization"]
        if organization_id and organization["id"] != organization_id:
            continue
        if (
            needle
            and needle
            not in " ".join([organization["code"], organization["name"], organization["short_name"]]).casefold()
        ):
            continue
        if dimension_filters:
            requirements = [
                requirement
                for requirement in item["requirements"]
                if all(
                    requirement["dimensions"].get(key, "").casefold() == value.casefold()
                    for key, value in dimension_filters.items()
                )
            ]
            if not requirements:
                continue
            required = sum(requirement["required"] for requirement in requirements)
            completed = sum(min(requirement["received"], requirement["required"]) for requirement in requirements)
            statuses = {requirement["status"] for requirement in requirements}
            if statuses <= {"submitted"}:
                item_status = "submitted"
            elif statuses <= {"submitted", "overdue"} and "overdue" in statuses:
                item_status = "overdue"
            elif any(requirement["received"] > 0 for requirement in requirements):
                item_status = "partial"
            else:
                item_status = "not_submitted"
            item.update(
                {
                    "requirements": requirements,
                    "required": required,
                    "completed": completed,
                    "percent": round(completed / required * 100, 1) if required else 100.0,
                    "status": item_status,
                    "last_received_at": max(
                        (
                            requirement["last_received_at"]
                            for requirement in requirements
                            if requirement["last_received_at"] is not None
                        ),
                        default=None,
                    ),
                }
            )
        if status_filter and item["status"] != status_filter:
            continue
        organizations.append(item)

    result["organizations"] = organizations
    result["required"] = sum(item["required"] for item in organizations)
    result["completed"] = sum(item["completed"] for item in organizations)
    result["percent"] = round(result["completed"] / result["required"] * 100, 1) if result["required"] else 100.0
    counts: dict[str, int] = defaultdict(int)
    for item in organizations:
        counts[item["status"]] += 1
    result["counts"] = {
        "submitted": counts["submitted"],
        "partial": counts["partial"],
        "not_submitted": counts["not_submitted"],
        "overdue": counts["overdue"],
    }
    return result


async def build_progress(session: AsyncSession, campaign_id: int) -> dict[str, Any] | None:
    campaign = await _campaign_graph(session, campaign_id)
    if campaign is None:
        return None
    facts = await _file_facts(session, campaign_id)
    by_org: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for fact in facts:
        if fact["organization_id"] is not None:
            by_org[fact["organization_id"]].append(fact)

    requirements_by_org: dict[int, list[Requirement]] = defaultdict(list)
    for requirement in campaign.requirements:
        if requirement.required:
            requirements_by_org[requirement.organization_id].append(requirement)

    organizations: list[dict[str, Any]] = []
    total_required = 0
    total_completed = 0
    for link in campaign.organizations:
        if not link.included:
            continue
        org = link.organization
        org_facts = by_org.get(org.id, [])
        requirements = requirements_by_org.get(org.id, [])
        detail: list[dict[str, Any]] = []
        if requirements:
            for requirement in requirements:
                matching = [
                    fact
                    for fact in org_facts
                    if all(
                        fact["dimensions"].get(key, "").casefold() == value.casefold()
                        for key, value in requirement.dimensions.items()
                    )
                ]
                status = requirement_status(requirement, matching, campaign.deadline_at)
                completed_units = min(len(matching), requirement.required_file_count)
                total_required += requirement.required_file_count
                total_completed += completed_units
                detail.append(
                    {
                        "id": requirement.id,
                        "dimensions": requirement.dimensions,
                        "required": requirement.required_file_count,
                        "received": len(matching),
                        "first_received_at": min((item["first_received_at"] for item in matching), default=None),
                        "last_received_at": max((item["last_received_at"] for item in matching), default=None),
                        "deadline_at": requirement.deadline_at or campaign.deadline_at,
                        "status": status,
                    }
                )
            statuses = {item["status"] for item in detail}
            if statuses <= {"submitted"}:
                org_status = "submitted"
            elif statuses <= {"submitted", "overdue"} and "overdue" in statuses:
                org_status = "overdue"
            elif any(item["received"] > 0 for item in detail):
                org_status = "partial"
            else:
                org_status = "not_submitted"
            required_count = sum(item["required"] for item in detail)
            completed_count = sum(min(item["received"], item["required"]) for item in detail)
        else:
            required_count = 1
            completed_count = 1 if org_facts else 0
            total_required += 1
            total_completed += completed_count
            org_status = "submitted" if org_facts else "not_submitted"
        organizations.append(
            {
                "organization": {
                    "id": org.id,
                    "code": org.code,
                    "name": org.name,
                    "short_name": org.short_name,
                    "folder_name": org.folder_name,
                },
                "completed": completed_count,
                "required": required_count,
                "percent": round(completed_count / required_count * 100, 1) if required_count else 100.0,
                "last_received_at": max((item["last_received_at"] for item in org_facts), default=None),
                "status": org_status,
                "requirements": detail,
            }
        )

    counts: dict[str, int] = defaultdict(int)
    for item in organizations:
        counts[item["status"]] += 1
    return {
        "campaign_id": campaign.id,
        "completed": total_completed,
        "required": total_required,
        "percent": round(total_completed / total_required * 100, 1) if total_required else 100.0,
        "counts": {
            "submitted": counts["submitted"],
            "partial": counts["partial"],
            "not_submitted": counts["not_submitted"],
            "overdue": counts["overdue"],
        },
        "organizations": organizations,
    }


async def build_summary(session: AsyncSession, campaign_id: int) -> dict[str, Any] | None:
    progress = await build_progress(session, campaign_id)
    if progress is None:
        return None
    today = datetime.now(UTC).date()
    file_rows = (
        await session.execute(
            select(
                func.count(FileRecord.id),
                func.sum(case((func.date(FileRecord.first_received_at) == today.isoformat(), 1), else_=0)),
                func.sum(case((FileRecord.parse_status != ParseStatus.OK, 1), else_=0)),
                func.sum(case((FileRecord.deleted.is_(True), 1), else_=0)),
            ).where(FileRecord.campaign_id == campaign_id)
        )
    ).one()
    progress["files"] = {
        "total": int(file_rows[0] or 0),
        "today": int(file_rows[1] or 0),
        "unrecognized": int(file_rows[2] or 0),
        "deleted": int(file_rows[3] or 0),
    }
    return progress


async def build_matrix(
    session: AsyncSession,
    campaign_id: int,
    column_dimension: str,
    filter_dimension: str | None = None,
    filter_value: str | None = None,
) -> dict[str, Any] | None:
    progress = await build_progress(session, campaign_id)
    if progress is None:
        return None
    columns: set[str] = set()
    rows: list[dict[str, Any]] = []
    for org in progress["organizations"]:
        cells: dict[str, str] = {}
        for requirement in org["requirements"]:
            dimensions = requirement["dimensions"]
            if filter_dimension and filter_value and dimensions.get(filter_dimension) != filter_value:
                continue
            column = dimensions.get(column_dimension)
            if not column:
                continue
            columns.add(column)
            current = cells.get(column)
            status = requirement["status"]
            precedence = {"not_submitted": 4, "partial": 3, "overdue": 2, "submitted": 1}
            if current is None or precedence[status] > precedence[current]:
                cells[column] = status
        rows.append({"organization": org["organization"], "cells": cells})
    return {"columns": sorted(columns), "rows": rows}
