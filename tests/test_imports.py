from __future__ import annotations

import pytest
from sqlalchemy import func, select

from file_control.db import SessionFactory
from file_control.models import Organization
from file_control.services.imports import import_organizations, read_tabular


@pytest.mark.asyncio
async def test_cp1251_csv_preview_and_commit() -> None:
    content = "Код;Название;Имя папки;Email\nORG-1;ООО Ромашка;Ромашка;mail@example.test\n".encode("cp1251")
    rows = read_tabular("organizations.csv", content)

    async with SessionFactory() as session:
        preview = await import_organizations(session, rows, commit=False)
        assert preview["to_add"] == 1
        assert preview["errors"] == []
        assert await session.scalar(select(func.count(Organization.id))) == 0

        result = await import_organizations(session, rows, commit=True)
        assert result["committed"] is True
        organization = await session.scalar(select(Organization))
        assert organization.code == "ORG-1"
        assert organization.folder_name == "Ромашка"
