"""Isolated provider bridge using JSON on stdin/stdout and no shell."""
import asyncio
import json
import os
from pathlib import Path
import signal
import logging
import re

from jmemby.domain.errors import JobError
from jmemby.domain.models import Chapter, Manga, Page, normalize_tags


class CommandProvider:
    source = "jmcomic"

    def child_environment(self):
        return {key: value for key, value in os.environ.items()
                if not key.startswith(("TELEGRAM_", "KOMGA_"))}

    def error_message(self, code):
        return {
            "TIMEOUT": "禁漫请求超时，请检查 VPS 到数据源的连接。",
            "DISK_FULL": "磁盘已满，释放空间后可手动重试。",
            "SIZE_LIMIT": "下载内容超出大小限制。",
            "NETWORK": "禁漫网络连接失败，请检查 DNS、代理及 TLS 连接。",
            "RATE_LIMIT": "禁漫请求被限流，请稍后重试。",
            "SERVER_ERROR": "禁漫服务暂时不可用，请稍后重试。",
            "JM_ACCESS_DENIED": "禁漫拒绝访问（401/403），请检查数据源访问限制。",
            "JM_NOT_FOUND": "数据源未找到内容或接口，请核对漫画 ID 和 API 域名。",
            "JM_SDK_INCOMPATIBLE": "JMComic SDK 接口不兼容或依赖缺失，请查看 provider_error 日志。",
            "JM_CONFIG_ERROR": "JMComic 客户端初始化失败，请查看 provider_error 日志。",
            "JM_RESPONSE_UNEXPECTED": "禁漫返回了 SDK 不接受的响应，请稍后重试；若持续发生，请检查 API 域名及 provider_error 日志。",
            "JM_CLIENT_ERROR": "JMComic 请求失败，请查看 provider_error 日志中的阶段和异常类型。",
        }.get(code, "数据源请求失败，请查看 provider_error 日志。")

    def __init__(self, command: str, timeout: int = 300):
        self.argv = json.loads(command)
        if not isinstance(self.argv, list) or not self.argv or not all(isinstance(x, str) for x in self.argv):
            raise ValueError("PROVIDER_COMMAND must be a nonempty JSON argv list")
        self.timeout = timeout

    async def request(self, request: dict, timeout: int | None = None) -> dict:
        kwargs = {"start_new_session": True} if os.name != "nt" else {}
        process = await asyncio.create_subprocess_exec(
            *self.argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=self.child_environment(), **kwargs,
        )
        try:
            async with asyncio.timeout(self.timeout if timeout is None else timeout):
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
                if not isinstance(error, dict):
                    raise ValueError("error must be an object")
                code = error.get("code", "PROVIDER_FAILED")
                if not isinstance(code, str) or not re.fullmatch(r"[A-Z_]{1,64}", code):
                    code = "PROVIDER_FAILED"
                diagnostic = error.get("diagnostic", {})
                safe = {}
                if isinstance(diagnostic, dict):
                    for key in ("phase", "exceptions", "http_status", "sdk_version", "frames"):
                        value = diagnostic.get(key)
                        values = value if isinstance(value, list) else [value]
                        safe[key] = [str(v)[:120] for v in values[:6]
                                     if v is not None and re.fullmatch(r"[\w.:-]{1,120}", str(v))]
                logging.getLogger(__name__).warning("provider_error code=%s diagnostic=%s", code, json.dumps(safe))
                retryable = code in {"TIMEOUT", "RATE_LIMIT", "NETWORK", "SERVER_ERROR", "JM_RESPONSE_UNEXPECTED"}
                raise JobError(code, f"[{code}] {self.error_message(code)}", retryable,
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
                         tuple(str(a) for a in result.get("authors", [])), chapters,
                         tags=normalize_tags(result.get("tags")))
        except (KeyError, TypeError, ValueError) as exc:
            raise JobError("PROVIDER_PROTOCOL", "数据源章节信息不完整") from exc

    async def get_pages(self, chapter_id: str) -> tuple[Page, ...]:
        result = await self.request({"operation": "pages", "chapter_id": chapter_id})
        try:
            if not isinstance(result.get("pages"), list) or not all(isinstance(url, str) and url for url in result["pages"]):
                raise ValueError("pages must be a list of nonempty strings")
            return tuple(Page(i, str(url)) for i, url in enumerate(result["pages"], 1))
        except (KeyError, TypeError, ValueError) as exc:
            raise JobError("PROVIDER_PROTOCOL", "数据源页清单不正确") from exc

    async def download(self, page: Page, destination: Path, max_bytes: int):
        await self.request({"operation": "download", "url": page.url,
                            "destination": str(destination.resolve()), "max_bytes": max_bytes})
