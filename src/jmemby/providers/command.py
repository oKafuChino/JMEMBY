"""Isolated provider bridge using JSON on stdin/stdout and no shell."""
import asyncio
import json
import os
from pathlib import Path
import signal

from jmemby.domain.errors import JobError
from jmemby.domain.models import Chapter, Manga, Page


class CommandProvider:
    source = "jmcomic"

    def __init__(self, command: str, timeout: int = 300):
        self.argv = json.loads(command)
        if not isinstance(self.argv, list) or not self.argv or not all(isinstance(x, str) for x in self.argv):
            raise ValueError("PROVIDER_COMMAND must be a nonempty JSON argv list")
        self.timeout = timeout

    async def request(self, request: dict) -> dict:
        kwargs = {"start_new_session": True} if os.name != "nt" else {}
        process = await asyncio.create_subprocess_exec(
            *self.argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, **kwargs,
        )
        try:
            async with asyncio.timeout(self.timeout):
                process.stdin.write(json.dumps(request).encode() + b"\n")
                await process.stdin.drain()
                process.stdin.close()
                chunks, total = [], 0
                while data := await process.stdout.read(65536):
                    total += len(data)
                    if total > 8 * 1024 * 1024:
                        raise JobError("PROVIDER_PROTOCOL", "数据源输出超出限制")
                    chunks.append(data)
                await process.wait()
            if process.returncode:
                raise JobError("PROVIDER_FAILED", "数据源进程执行失败", True)
            result = json.loads(b"".join(chunks))
            if not isinstance(result, dict):
                raise ValueError("object expected")
            if "error" in result:
                error = result["error"]
                code = error.get("code", "PROVIDER_FAILED")
                retryable = code in {"TIMEOUT", "RATE_LIMIT", "NETWORK", "SERVER_ERROR"}
                raise JobError(code, "数据源请求失败，请检查配置或稍后重试", retryable,
                               min(max(float(error.get("retry_after", 0)), 0), 86400))
            return result
        except TimeoutError as exc:
            raise JobError("TIMEOUT", "数据源请求超时", True) from exc
        except (ValueError, KeyError, TypeError) as exc:
            raise JobError("PROVIDER_PROTOCOL", "数据源返回格式不正确") from exc
        finally:
            if process.returncode is None:
                if os.name != "nt":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    process.kill()
                await process.wait()

    async def get_manga(self, source_id: str) -> Manga:
        result = await self.request({"operation": "metadata", "id": source_id})
        try:
            chapters = tuple(Chapter(str(c["id"]), index, str(c["title"]), ())
                             for index, c in enumerate(result["chapters"], 1))
            if not chapters or len({c.source_id for c in chapters}) != len(chapters):
                raise ValueError("empty or duplicated chapters")
            return Manga(self.source, source_id, str(result["title"]),
                         tuple(str(a) for a in result.get("authors", [])), chapters)
        except (KeyError, TypeError, ValueError) as exc:
            raise JobError("PROVIDER_PROTOCOL", "数据源章节信息不完整") from exc

    async def get_pages(self, chapter_id: str) -> tuple[Page, ...]:
        result = await self.request({"operation": "pages", "chapter_id": chapter_id})
        try:
            return tuple(Page(i, str(url)) for i, url in enumerate(result["pages"], 1))
        except (KeyError, TypeError) as exc:
            raise JobError("PROVIDER_PROTOCOL", "数据源页清单不正确") from exc

    async def download(self, page: Page, destination: Path, max_bytes: int):
        await self.request({"operation": "download", "url": page.url,
                            "destination": str(destination.resolve()), "max_bytes": max_bytes})
