"""Version-specific Komga operations supplied by a verified external adapter.

This intentionally shares the cancellable JSON process transport. There are no
guessed HTTP endpoints in the application. See PROVIDER_PROTOCOL.md.
"""
from jmemby.providers.command import CommandProvider


class KomgaBridge(CommandProvider):
    async def import_paths(self, paths):
        result = await self.request({"operation": "import", "paths": paths})
        books = result.get("books", {})
        if not isinstance(books, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in books.items()):
            raise ValueError("invalid import response")
        return books
