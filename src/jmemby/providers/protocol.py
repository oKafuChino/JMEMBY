from __future__ import annotations

from typing import Protocol
from pathlib import Path
from jmemby.domain.models import Manga, Page


class MangaProvider(Protocol):
    source: str

    async def get_manga(self, source_id: str) -> Manga: ...
    async def get_pages(self, chapter_id: str) -> tuple[Page, ...]: ...
    async def download(self, page: Page, destination: Path, max_bytes: int) -> None: ...
