from __future__ import annotations

import enum
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
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


class WatchStatus(str, enum.Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    ARCHIVED = "archived"


class EventType(str, enum.Enum):
    CREATED = "created"
    UPDATED = "updated"
    RENAMED = "renamed"
    MOVED = "moved"
    COPIED = "copied"
    DELETED = "deleted"


class ReportCadence(str, enum.Enum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


class Watch(Base):
    __tablename__ = "watches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(500), index=True)
    root_path: Mapped[str] = mapped_column(String(4096))
    owner_user_id: Mapped[str] = mapped_column(String(255), index=True)
    scope_type: Mapped[str] = mapped_column(String(20), default="personal", index=True)
    group_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[WatchStatus] = mapped_column(
        Enum(WatchStatus, native_enum=False), default=WatchStatus.ACTIVE, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    created_by: Mapped[str] = mapped_column(String(255))
    last_webhook_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    files: Mapped[list[FileRecord]] = relationship(back_populates="watch", cascade="all, delete-orphan")
    events: Mapped[list[FileEvent]] = relationship(back_populates="watch", cascade="all, delete-orphan")
    activity_events: Mapped[list[ActivityEvent]] = relationship(back_populates="watch", cascade="all, delete-orphan")
    reconciliation_runs: Mapped[list[ReconciliationRun]] = relationship(
        back_populates="watch", cascade="all, delete-orphan"
    )
    folders: Mapped[list[FolderRecord]] = relationship(back_populates="watch", cascade="all, delete-orphan")
    report_schedule: Mapped[ReportSchedule | None] = relationship(
        back_populates="watch", cascade="all, delete-orphan", uselist=False
    )

    __table_args__ = (
        Index("ix_watch_owner_status", "owner_user_id", "status"),
        Index("ix_watch_owner_root", "owner_user_id", "root_path"),
    )


class FileRecord(Base):
    __tablename__ = "watch_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    watch_id: Mapped[int] = mapped_column(ForeignKey("watches.id", ondelete="CASCADE"), index=True)
    nextcloud_file_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    filename: Mapped[str] = mapped_column(String(1024))
    full_path: Mapped[str] = mapped_column(String(4096))
    relative_path: Mapped[str] = mapped_column(String(4096))
    parent_path: Mapped[str] = mapped_column(String(4096), default="")
    mime_type: Mapped[str] = mapped_column(String(255), default="application/octet-stream")
    extension: Mapped[str] = mapped_column(String(100), default="", index=True)
    size: Mapped[int] = mapped_column(BigInteger, default=0)
    etag: Mapped[str] = mapped_column(String(512), default="")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    nextcloud_upload_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    nextcloud_mtime: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exists: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revision_count: Mapped[int] = mapped_column(Integer, default=0)
    discovered_by: Mapped[str] = mapped_column(String(30), default="event")

    watch: Mapped[Watch] = relationship(back_populates="files")
    events: Mapped[list[FileEvent]] = relationship(back_populates="file", cascade="all, delete-orphan")
    activity_events: Mapped[list[ActivityEvent]] = relationship(back_populates="file")

    __table_args__ = (
        UniqueConstraint("watch_id", "nextcloud_file_id"),
        Index("ix_watch_file_path", "watch_id", "full_path"),
        Index("ix_watch_file_relative", "watch_id", "relative_path"),
        Index("ix_watch_file_state_seen", "watch_id", "exists", "first_seen_at"),
    )


class FolderRecord(Base):
    __tablename__ = "watch_folders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    watch_id: Mapped[int] = mapped_column(ForeignKey("watches.id", ondelete="CASCADE"), index=True)
    nextcloud_file_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(1024))
    full_path: Mapped[str] = mapped_column(String(4096))
    relative_path: Mapped[str] = mapped_column(String(4096))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    exists: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    watch: Mapped[Watch] = relationship(back_populates="folders")

    __table_args__ = (
        UniqueConstraint("watch_id", "nextcloud_file_id"),
        Index("ix_watch_folder_path", "watch_id", "full_path"),
        Index("ix_watch_folder_relative", "watch_id", "relative_path"),
    )


class FileEvent(Base):
    __tablename__ = "watch_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    watch_id: Mapped[int] = mapped_column(ForeignKey("watches.id", ondelete="CASCADE"), index=True)
    file_id: Mapped[int] = mapped_column(ForeignKey("watch_files.id", ondelete="CASCADE"), index=True)
    event_key: Mapped[str] = mapped_column(String(64), unique=True)
    event_type: Mapped[EventType] = mapped_column(Enum(EventType, native_enum=False), index=True)
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    path: Mapped[str] = mapped_column(String(4096), default="")
    old_path: Mapped[str] = mapped_column(String(4096), default="")
    new_path: Mapped[str] = mapped_column(String(4096), default="")
    size: Mapped[int] = mapped_column(BigInteger, default=0)
    etag: Mapped[str] = mapped_column(String(512), default="")
    actor_user_id: Mapped[str] = mapped_column(String(255), default="")
    raw_event: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    watch: Mapped[Watch] = relationship(back_populates="events")
    file: Mapped[FileRecord] = relationship(back_populates="events")

    __table_args__ = (
        Index("ix_watch_event_time", "watch_id", "event_time"),
        Index("ix_watch_event_file_time", "file_id", "event_time"),
    )


class ActivityEvent(Base):
    """User-visible activity stream.

    This separate table allows folder events without pretending that a folder is
    a file. The older ``watch_events`` table is kept as a recoverable archive.
    """

    __tablename__ = "watch_activity_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    watch_id: Mapped[int] = mapped_column(ForeignKey("watches.id", ondelete="CASCADE"), index=True)
    file_id: Mapped[int | None] = mapped_column(
        ForeignKey("watch_files.id", ondelete="SET NULL"), nullable=True, index=True
    )
    event_key: Mapped[str] = mapped_column(String(64), unique=True)
    event_type: Mapped[EventType] = mapped_column(Enum(EventType, native_enum=False), index=True)
    node_type: Mapped[str] = mapped_column(String(20), default="file", index=True)
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    path: Mapped[str] = mapped_column(String(4096), default="")
    old_path: Mapped[str] = mapped_column(String(4096), default="")
    new_path: Mapped[str] = mapped_column(String(4096), default="")
    size: Mapped[int] = mapped_column(BigInteger, default=0)
    etag: Mapped[str] = mapped_column(String(512), default="")
    actor_user_id: Mapped[str] = mapped_column(String(255), default="")
    raw_event: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    watch: Mapped[Watch] = relationship(back_populates="activity_events")
    file: Mapped[FileRecord | None] = relationship(back_populates="activity_events")

    __table_args__ = (
        Index("ix_watch_activity_time", "watch_id", "event_time"),
        Index("ix_watch_activity_file_time", "file_id", "event_time"),
    )


class ReconciliationRun(Base):
    __tablename__ = "watch_reconciliation_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    watch_id: Mapped[int] = mapped_column(ForeignKey("watches.id", ondelete="CASCADE"), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="running", index=True)
    checked_count: Mapped[int] = mapped_column(Integer, default=0)
    created_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_count: Mapped[int] = mapped_column(Integer, default=0)
    missing_count: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    triggered_by: Mapped[str] = mapped_column(String(255), default="system")

    watch: Mapped[Watch] = relationship(back_populates="reconciliation_runs")


class ReportSchedule(Base):
    __tablename__ = "watch_report_schedules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    watch_id: Mapped[int] = mapped_column(ForeignKey("watches.id", ondelete="CASCADE"), unique=True, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    cadence: Mapped[ReportCadence] = mapped_column(Enum(ReportCadence, native_enum=False), default=ReportCadence.DAILY)
    local_time: Mapped[str] = mapped_column(String(5), default="09:00")
    weekday: Mapped[int] = mapped_column(Integer, default=0)
    day_of_month: Mapped[int] = mapped_column(Integer, default=1)
    destination_path: Mapped[str] = mapped_column(String(4096), default="")
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status: Mapped[str] = mapped_column(String(30), default="never")
    last_error: Mapped[str] = mapped_column(Text, default="")
    last_file_path: Mapped[str] = mapped_column(String(4096), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    watch: Mapped[Watch] = relationship(back_populates="report_schedule")
