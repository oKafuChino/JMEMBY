from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class TaskStatus(StrEnum):
    QUEUED = "QUEUED"
    DOWNLOADING = "DOWNLOADING"
    PACKAGING = "PACKAGING"
    IMPORT_PENDING = "IMPORT_PENDING"
    COMPLETED = "COMPLETED"
    RETRY_WAIT = "RETRY_WAIT"
    PAUSED_DISK = "PAUSED_DISK"
    FAILED = "FAILED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"


class ArtifactStatus(StrEnum):
    MISSING = "MISSING"
    STAGED = "STAGED"
    PUBLISHED = "PUBLISHED"
    IMPORTED = "IMPORTED"
    INVALID = "INVALID"


@dataclass(frozen=True, slots=True)
class Page:
    number: int
    url: str
    expected_name: str | None = None


@dataclass(frozen=True, slots=True)
class Chapter:
    source_id: str
    ordinal: int
    title: str
    pages: tuple[Page, ...]


@dataclass(frozen=True, slots=True)
class Manga:
    source: str
    source_id: str
    title: str
    authors: tuple[str, ...] = ()
    chapters: tuple[Chapter, ...] = ()
    cover_url: str | None = None


@dataclass(slots=True)
class Progress:
    chapter_index: int = 0
    chapter_count: int = 0
    page: int = 0
    pages: int = 0


@dataclass(slots=True)
class Task:
    id: str
    manga: Manga
    requester_user_id: int
    chat_id: int
    status: TaskStatus = TaskStatus.QUEUED
    phase: str = "queued"
    progress: Progress = field(default_factory=Progress)
    error_code: str | None = None
    safe_error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
