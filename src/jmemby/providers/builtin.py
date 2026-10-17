"""Batch capability only for the bundled JM adapter, not custom commands."""
from jmemby.providers.command import CommandProvider


class JmProvider(CommandProvider):
    async def download_batch(self, pages, stage, max_bytes, max_pixels, budget):
        await self.request({
            "operation": "download_batch",
            "pages": [{"url": p.url, "number": p.number} for p in pages],
            "directory": str(stage.resolve()), "max_bytes": max_bytes,
            "max_pixels": max_pixels, "budget": budget,
        }, timeout=min(3600, self.timeout * max(1, len(pages))))
