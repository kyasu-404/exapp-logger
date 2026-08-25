from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .models import CampaignStatus, StructureMode


class APIModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, use_enum_values=True)


class OrganizationCreate(APIModel):
    code: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=500)
    short_name: str = Field(default="", max_length=255)
    folder_name: str = Field(min_length=1, max_length=255)
    email: str = Field(default="", max_length=320)
    active: bool = True
    extra_fields: dict[str, Any] = Field(default_factory=dict)

    @field_validator("code", "name", "short_name", "folder_name", "email")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()


class OrganizationUpdate(APIModel):
    code: str | None = Field(default=None, min_length=1, max_length=100)
    name: str | None = Field(default=None, min_length=1, max_length=500)
    short_name: str | None = Field(default=None, max_length=255)
    folder_name: str | None = Field(default=None, min_length=1, max_length=255)
    email: str | None = Field(default=None, max_length=320)
    active: bool | None = None
    extra_fields: dict[str, Any] | None = None


class DimensionValueInput(APIModel):
    value: str = Field(min_length=1, max_length=500)
    display_name: str | None = Field(default=None, max_length=500)
    sort_order: int = 0
    active: bool = True


class DimensionInput(APIModel):
    key: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_\-]*$", max_length=100)
    name: str = Field(min_length=1, max_length=255)
    required: bool = True
    fixed: bool = True
    values: list[DimensionValueInput] = Field(default_factory=list)


class CampaignCreate(APIModel):
    name: str = Field(min_length=1, max_length=500)
    campaign_type: str = Field(default="collection", max_length=100)
    year: int = Field(ge=2000, le=2200)
    description: str = ""
    root_path: str = Field(min_length=1, max_length=2048)
    storage_user_id: str | None = Field(default=None, max_length=255)
    start_at: datetime | None = None
    deadline_at: datetime | None = None
    status: CampaignStatus = CampaignStatus.DRAFT
    structure_mode: StructureMode = StructureMode.STRICT
    requirements_mode: Literal["simple", "matrix"] = "simple"
    organization_ids: list[int] = Field(default_factory=list)
    dimensions: list[DimensionInput] = Field(default_factory=list)
    settings: dict[str, Any] = Field(default_factory=dict)

    @field_validator("root_path")
    @classmethod
    def normalize_root(cls, value: str) -> str:
        normalized = "/" + value.strip().replace("\\", "/").strip("/")
        if normalized == "/":
            raise ValueError("Корневая папка кампании не может быть корнем хранилища")
        return normalized


class CampaignUpdate(APIModel):
    name: str | None = Field(default=None, min_length=1, max_length=500)
    campaign_type: str | None = Field(default=None, max_length=100)
    year: int | None = Field(default=None, ge=2000, le=2200)
    description: str | None = None
    root_path: str | None = Field(default=None, min_length=1, max_length=2048)
    storage_user_id: str | None = Field(default=None, max_length=255)
    start_at: datetime | None = None
    deadline_at: datetime | None = None
    status: CampaignStatus | None = None
    structure_mode: StructureMode | None = None
    requirements_mode: Literal["simple", "matrix"] | None = None
    organization_ids: list[int] | None = None
    dimensions: list[DimensionInput] | None = None
    settings: dict[str, Any] | None = None
    confirm_reclassification: bool = False


class CampaignClone(APIModel):
    name: str = Field(min_length=1, max_length=500)
    year: int = Field(ge=2000, le=2200)
    root_path: str = Field(min_length=1, max_length=2048)
    start_at: datetime | None = None
    deadline_at: datetime | None = None
    copy_requirements: bool = True


class RequirementInput(APIModel):
    organization_id: int
    dimensions: dict[str, str] = Field(default_factory=dict)
    required: bool = True
    required_file_count: int = Field(default=1, ge=1, le=1000)
    deadline_at: datetime | None = None


class Page(APIModel):
    items: list[Any]
    page: int
    page_size: int
    total: int
    pages: int


class ImportPreview(APIModel):
    rows: int
    to_add: int
    to_update: int
    errors: list[dict[str, Any]]
    preview: list[dict[str, Any]]
