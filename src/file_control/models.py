from __future__ import annotations

import enum
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class CampaignStatus(str, enum.Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    COMPLETED = "completed"
    ARCHIVED = "archived"


class StructureMode(str, enum.Enum):
    STRICT = "strict"
    FREE = "free"


class ParseStatus(str, enum.Enum):
    OK = "ok"
    UNKNOWN_ORGANIZATION = "unknown_organization"
    UNKNOWN_DIMENSION = "unknown_dimension"
    MISSING_DIMENSION = "missing_dimension"
    OUTSIDE_STRUCTURE = "outside_structure"
    IGNORED = "ignored"


class EventType(str, enum.Enum):
    CREATED = "created"
    WRITTEN = "written"
    RENAMED = "renamed"
    DELETED = "deleted"
    COPIED = "copied"
    RECONCILED_CREATED = "reconciled_created"
    RECONCILED_UPDATED = "reconciled_updated"
    RECONCILED_MISSING = "reconciled_missing"
    RECLASSIFIED = "reclassified"


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(500))
    short_name: Mapped[str] = mapped_column(String(255), default="")
    folder_name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(320), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    extra_fields: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    campaigns: Mapped[list[CampaignOrganization]] = relationship(back_populates="organization")
    files: Mapped[list[FileRecord]] = relationship(back_populates="organization")


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(500), index=True)
    campaign_type: Mapped[str] = mapped_column(String(100), default="collection")
    year: Mapped[int] = mapped_column(Integer, index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    root_path: Mapped[str] = mapped_column(String(2048))
    storage_user_id: Mapped[str] = mapped_column(String(255), index=True)
    start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[CampaignStatus] = mapped_column(
        Enum(CampaignStatus, native_enum=False), default=CampaignStatus.DRAFT, index=True
    )
    structure_mode: Mapped[StructureMode] = mapped_column(
        Enum(StructureMode, native_enum=False), default=StructureMode.STRICT
    )
    requirements_mode: Mapped[str] = mapped_column(String(30), default="simple")
    settings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    created_by: Mapped[str] = mapped_column(String(255))

    organizations: Mapped[list[CampaignOrganization]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan"
    )
    dimensions: Mapped[list[Dimension]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan", order_by="Dimension.position"
    )
    requirements: Mapped[list[Requirement]] = relationship(back_populates="campaign", cascade="all, delete-orphan")
    files: Mapped[list[FileRecord]] = relationship(back_populates="campaign", cascade="all, delete-orphan")
    reconciliation_runs: Mapped[list[ReconciliationRun]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan"
    )

    __table_args__ = (Index("ix_campaign_status_year", "status", "year"),)


class CampaignOrganization(Base):
    __tablename__ = "campaign_organizations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), index=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="RESTRICT"), index=True)
    included: Mapped[bool] = mapped_column(Boolean, default=True)

    campaign: Mapped[Campaign] = relationship(back_populates="organizations")
    organization: Mapped[Organization] = relationship(back_populates="campaigns")

    __table_args__ = (UniqueConstraint("campaign_id", "organization_id"),)


class Dimension(Base):
    __tablename__ = "dimensions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    key: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(255))
    required: Mapped[bool] = mapped_column(Boolean, default=True)
    fixed: Mapped[bool] = mapped_column(Boolean, default=True)

    campaign: Mapped[Campaign] = relationship(back_populates="dimensions")
    values: Mapped[list[DimensionValue]] = relationship(
        back_populates="dimension", cascade="all, delete-orphan", order_by="DimensionValue.sort_order"
    )
    file_values: Mapped[list[FileDimensionValue]] = relationship(back_populates="dimension")

    __table_args__ = (
        UniqueConstraint("campaign_id", "key"),
        UniqueConstraint("campaign_id", "position"),
    )


class DimensionValue(Base):
    __tablename__ = "dimension_values"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dimension_id: Mapped[int] = mapped_column(ForeignKey("dimensions.id", ondelete="CASCADE"), index=True)
    value: Mapped[str] = mapped_column(String(500))
    display_name: Mapped[str] = mapped_column(String(500))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    dimension: Mapped[Dimension] = relationship(back_populates="values")
    file_values: Mapped[list[FileDimensionValue]] = relationship(back_populates="dimension_value")

    __table_args__ = (UniqueConstraint("dimension_id", "value"),)


class Requirement(Base):
    __tablename__ = "requirements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), index=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    identity_key: Mapped[str] = mapped_column(String(1024))
    dimensions: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    required: Mapped[bool] = mapped_column(Boolean, default=True)
    required_file_count: Mapped[int] = mapped_column(Integer, default=1)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    campaign: Mapped[Campaign] = relationship(back_populates="requirements")
    organization: Mapped[Organization] = relationship()

    __table_args__ = (UniqueConstraint("campaign_id", "organization_id", "identity_key"),)


class FileRecord(Base):
    __tablename__ = "file_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), index=True)
    organization_id: Mapped[int | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    nextcloud_file_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    owner_user_id: Mapped[str] = mapped_column(String(255), index=True)
    filename: Mapped[str] = mapped_column(String(1024))
    directory: Mapped[str] = mapped_column(String(2048))
    relative_path: Mapped[str] = mapped_column(String(2048))
    full_path: Mapped[str] = mapped_column(String(4096))
    mime_type: Mapped[str] = mapped_column(String(255), default="application/octet-stream")
    size: Mapped[int] = mapped_column(Integer, default=0)
    etag: Mapped[str] = mapped_column(String(512), default="")
    first_received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    last_received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    node_mtime: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    event_received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    revision_count: Mapped[int] = mapped_column(Integer, default=0)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    parse_status: Mapped[ParseStatus] = mapped_column(
        Enum(ParseStatus, native_enum=False), default=ParseStatus.OK, index=True
    )
    parse_message: Mapped[str] = mapped_column(String(1000), default="")
    warning_ignored: Mapped[bool] = mapped_column(Boolean, default=False)
    discovered_by: Mapped[str] = mapped_column(String(30), default="event")

    campaign: Mapped[Campaign] = relationship(back_populates="files")
    organization: Mapped[Organization | None] = relationship(back_populates="files")
    dimension_values: Mapped[list[FileDimensionValue]] = relationship(
        back_populates="file", cascade="all, delete-orphan"
    )
    events: Mapped[list[FileEvent]] = relationship(back_populates="file", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("campaign_id", "nextcloud_file_id"),
        Index("ix_file_campaign_path", "campaign_id", "full_path"),
        Index("ix_file_campaign_deleted_parse", "campaign_id", "deleted", "parse_status"),
    )


class FileDimensionValue(Base):
    __tablename__ = "file_dimension_values"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    file_id: Mapped[int] = mapped_column(ForeignKey("file_records.id", ondelete="CASCADE"), index=True)
    dimension_id: Mapped[int] = mapped_column(ForeignKey("dimensions.id", ondelete="CASCADE"), index=True)
    dimension_value_id: Mapped[int | None] = mapped_column(
        ForeignKey("dimension_values.id", ondelete="SET NULL"), nullable=True, index=True
    )
    raw_value: Mapped[str] = mapped_column(String(1000), default="")

    file: Mapped[FileRecord] = relationship(back_populates="dimension_values")
    dimension: Mapped[Dimension] = relationship(back_populates="file_values")
    dimension_value: Mapped[DimensionValue | None] = relationship(back_populates="file_values")

    __table_args__ = (UniqueConstraint("file_id", "dimension_id"),)


class FileEvent(Base):
    __tablename__ = "file_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    file_id: Mapped[int] = mapped_column(ForeignKey("file_records.id", ondelete="CASCADE"), index=True)
    event_key: Mapped[str] = mapped_column(String(64), unique=True)
    event_type: Mapped[EventType] = mapped_column(Enum(EventType, native_enum=False), index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    old_path: Mapped[str] = mapped_column(String(4096), default="")
    new_path: Mapped[str] = mapped_column(String(4096), default="")
    etag: Mapped[str] = mapped_column(String(512), default="")
    size: Mapped[int] = mapped_column(Integer, default=0)
    actor_user_id: Mapped[str] = mapped_column(String(255), default="")
    raw_event: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    file: Mapped[FileRecord] = relationship(back_populates="events")

    __table_args__ = (Index("ix_event_file_time", "file_id", "occurred_at"),)


class ReconciliationRun(Base):
    __tablename__ = "reconciliation_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="running", index=True)
    checked_count: Mapped[int] = mapped_column(Integer, default=0)
    created_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_count: Mapped[int] = mapped_column(Integer, default=0)
    missing_count: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    triggered_by: Mapped[str] = mapped_column(String(255), default="system")

    campaign: Mapped[Campaign] = relationship(back_populates="reconciliation_runs")
