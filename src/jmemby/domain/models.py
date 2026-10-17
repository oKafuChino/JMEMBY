from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


def normalize_tags(value) -> tuple[str, ...]:
    """Optional source metadata must not break a download or produce invalid XML."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return ()
    result = []
    for tag in value[:100]:
        if not isinstance(tag, str):
            continue
        tag = "".join(c for c in tag if c in "\t\n\r" or
                      0x20 <= ord(c) <= 0xD7FF or 0xE000 <= ord(c) <= 0xFFFD or
                      0x10000 <= ord(c) <= 0x10FFFF)
        tag = " ".join(tag.split())[:100]
        if tag and tag not in result:
            result.append(tag)
    return tuple(result)


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
    tags: tuple[str, ...] = ()


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
