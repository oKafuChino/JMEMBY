from __future__ import annotations

from typing import Protocol
from pathlib import Path
from jmemby.domain.models import Manga, Page
from jmemby.domain.discovery import DiscoveryDetail


class MangaProvider(Protocol):
    source: str

    async def get_manga(self, source_id: str) -> Manga: ...
    async def get_pages(self, chapter_id: str) -> tuple[Page, ...]: ...
    async def download(self, page: Page, destination: Path, max_bytes: int) -> None: ...


class MangaDiscoveryProvider(MangaProvider, Protocol):
    """Optional extension; unsupported adapters must explicitly fail closed."""

    async def search_tag(self, tag: str, page: int) -> dict: ...
    async def get_discovery_detail(self, source_id: str) -> DiscoveryDetail: ...
