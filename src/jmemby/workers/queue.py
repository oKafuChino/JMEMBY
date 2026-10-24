import asyncio
from dataclasses import asdict, replace
import errno
import json
import logging
from pathlib import Path
import random
import shutil
import time

from jmemby.db.store import decode_manga
from jmemby.domain.errors import Cancelled, DiskLow, JobError
from jmemby.domain.models import Page
from jmemby.storage.cbz import contained, create_cbz, validate_cbz, validate_image, staged_pages

logger = logging.getLogger(__name__)


class Worker:
    def __init__(self, store, provider, komga, settings, active_tasks=None):
        self.store, self.provider, self.komga, self.settings = store, provider, komga, settings
        self.active_tasks = active_tasks if active_tasks is not None else set()
        self.root = Path(settings.storage_root).resolve()
        self.library = self.root / "library"
        self.staging = self.root / "staging"
        self.library.mkdir(parents=True, exist_ok=True)
        self.staging.mkdir(parents=True, exist_ok=True)
        self.heartbeat = time.monotonic()

    def check(self, task_id):
        self.heartbeat = time.monotonic()
        if self.store.get(task_id)["cancel_requested"]:
            raise Cancelled()
        if self.settings.disk_space_check_enabled and shutil.disk_usage(self.root).free < self.settings.min_free_space_gb * 1024**3:
            raise DiskLow()

    async def guarded(self, task_id, awaitable):
        operation = asyncio.ensure_future(awaitable)
        try:
            while not operation.done():
                self.check(task_id)
                await asyncio.wait({operation}, timeout=0.25)
            self.check(task_id)
            return await operation
        finally:
            if not operation.done():
                operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)

    async def run_once(self):
        self.heartbeat = time.monotonic()
        if not self.settings.disk_space_check_enabled or shutil.disk_usage(self.root).free >= self.settings.resume_free_space_gb * 1024**3:
            self.store.resume_disk()
        task = self.store.next_task(exclude=self.active_tasks)
        if task is None:
            return False
        self.active_tasks.add(task['id'])
        try:
            await self.execute(task)
            self.store.finish(task["id"], "COMPLETED")
        except Cancelled as exc:
            self.store.finish(task["id"], "CANCELLED", exc.code, str(exc))
        except DiskLow as exc:
            self.store.update(task["id"], status="PAUSED_DISK", error_code=exc.code, error=str(exc))
        except JobError as exc:
            attempts = self.store.get(task["id"])["attempts"] + 1
            message = str(exc)
            if self.store.get(task["id"])["phase"] == "import":
                message = "文件已保存，Komga 入库未确认。" + message
            if exc.retryable and attempts < self.settings.retry_max_attempts:
                delay = max(exc.delay, min(300, 2 ** attempts + random.random()))
                self.store.update(task["id"], status="RETRY_WAIT", attempts=attempts,
                                  next_at=time.time() + delay, error_code=exc.code, error=message)
            else:
                self.store.finish(task["id"], "FAILED", exc.code, message)
        except OSError as exc:
            if exc.errno == errno.ENOSPC:
                if self.settings.disk_space_check_enabled:
                    self.store.update(task["id"], status="PAUSED_DISK", error_code="DISK_LOW", error="磁盘已满")
                else:
                    self.store.finish(task["id"], "FAILED", "DISK_FULL", "磁盘已满，释放空间后可手动重试")
            else:
                self.store.finish(task["id"], "FAILED", "FILESYSTEM", "文件系统操作失败，请检查权限和磁盘")
        except asyncio.CancelledError:
            # On shutdown keep the durable phase for restart reconciliation.
            raise
        except Exception as exc:
            logger.error("task_failure task=%s exception_type=%s", task["id"], type(exc).__name__)
            self.store.finish(task["id"], "FAILED", "INTERNAL", "内部错误，请检查服务日志")
        finally:
            self.active_tasks.discard(task['id'])
        return True

    async def execute(self, task):
        task_id = task["id"]
        self.check(task_id)
        manga = decode_manga(task["snapshot"])
        records = self.store.chapters(task_id)
        chapter_map = {c.source_id: c for c in manga.chapters}
        total_bytes = 0
        for record in records:
            self.check(task_id)
            chapter = chapter_map[record["source_id"]]
            pages = tuple(Page(**p) for p in json.loads(record["pages"])) if record["pages"] else None
            if pages is None:
                pages = await self.guarded(task_id, self.provider.get_pages(chapter.source_id))
                if not pages or len(pages) > self.settings.max_chapter_pages:
                    raise JobError("PAGE_LIMIT", "章节为空或页数超出限制")
                self.store.update_chapter(record["id"], pages=json.dumps([asdict(p) for p in pages]))
            if (not pages or len(pages) > self.settings.max_chapter_pages
                    or [p.number for p in pages] != list(range(1, len(pages) + 1))):
                raise JobError("BAD_PAGES", "章节页清单为空、超限或页序无效")
            chapter = replace(chapter, pages=pages)
            target = contained(self.library, record["path"])
            stage = contained(self.staging, f"{task_id}/{record['id']}")
            was_existing = target.exists()
            if not target.exists():
                stage.mkdir(parents=True, exist_ok=True)
                self.store.update(task_id, status="DOWNLOADING", phase="download")
                semaphore = asyncio.Semaphore(self.settings.download_concurrency)
                if hasattr(self.provider, "download_batch"):
                    pending, staged_bytes = [], 0
                    for page in pages:
                        candidates = staged_pages(stage, page.number)
                        if len(candidates) > 1:
                            raise JobError("BAD_IMAGE", "存在重复页文件，请检查临时目录")
                        valid = False
                        if len(candidates) == 1:
                            try:
                                validate_image(candidates[0], self.settings.max_page_bytes, self.settings.max_pixels)
                                staged_bytes += candidates[0].stat().st_size
                                valid = True
                            except JobError:
                                candidates[0].unlink()
                        if not valid:
                            pending.append(page)
                    budget = self.settings.max_task_bytes - total_bytes - staged_bytes
                    if budget < 0:
                        raise JobError("SIZE_LIMIT", "任务体积超出限制")
                    if pending:
                        self.store.update(task_id, progress=f"章节 {chapter.ordinal}/{len(records)}：批量下载剩余 {len(pending)} 页")
                        await self.guarded(task_id, self.provider.download_batch(
                            pending, stage, self.settings.max_page_bytes, self.settings.max_pixels, budget))

                async def download(page):
                    async with semaphore:
                        nonlocal total_bytes
                        self.check(task_id)
                        candidates = staged_pages(stage, page.number)
                        if len(candidates) > 1:
                            raise JobError("BAD_IMAGE", "存在重复页文件，请检查临时目录")
                        existing = None
                        if len(candidates) == 1:
                            try:
                                validate_image(candidates[0], self.settings.max_page_bytes, self.settings.max_pixels)
                                existing = candidates[0]
                            except JobError:
                                candidates[0].unlink()
                        if existing is None:
                            partial = stage / f"{page.number:06d}.part"
                            try:
                                await self.guarded(task_id, self.provider.download(page, partial, self.settings.max_page_bytes))
                            except JobError as exc:
                                logger.warning("page_failure task=%s chapter=%s page=%s code=%s", task_id, record["id"], page.number, exc.code)
                                raise
                            extension = validate_image(partial, self.settings.max_page_bytes, self.settings.max_pixels)
                            existing = stage / f"{page.number:06d}{extension}"
                            partial.replace(existing)
                        total_bytes += existing.stat().st_size
                        if total_bytes > self.settings.max_task_bytes:
                            raise JobError("SIZE_LIMIT", "任务体积超出限制")
                        self.store.update(task_id, progress=f"章节 {chapter.ordinal}/{len(records)}，图片 {page.number}/{len(pages)}")

                operations = [asyncio.create_task(download(p)) for p in pages]
                try:
                    await asyncio.gather(*operations)
                finally:
                    for operation in operations:
                        if not operation.done():
                            operation.cancel()
                    await asyncio.gather(*operations, return_exceptions=True)
                self.check(task_id)
                # Packaging is synchronous and cannot be cancelled halfway through publication.
                self.store.update(task_id, status="PACKAGING")
                create_cbz(stage, target, manga, chapter, self.settings.max_page_bytes, self.settings.max_pixels)
            size, checksum = validate_cbz(target, manga, chapter, self.settings.max_page_bytes, self.settings.max_pixels)
            if was_existing:
                total_bytes += size
                if total_bytes > self.settings.max_task_bytes:
                    raise JobError("SIZE_LIMIT", "任务体积超出限制")
            if record["checksum"] and record["checksum"] != checksum:
                raise JobError("CHANGED_ARTIFACT", "漫画文件已被外部修改，请人工检查")
            self.store.update_chapter(record["id"], size=size, checksum=checksum, status="PUBLISHED")
        self.check(task_id)
        self.store.update(task_id, status="IMPORT_PENDING", phase="import")
        paths = [r["path"] for r in records]
        books = await self.guarded(task_id, self.komga.import_paths(paths))
        if set(books) != set(paths):
            raise JobError("KOMGA_PENDING", "尚未确认全部书籍", True)
        for record in records:
            self.store.update_chapter(record["id"], status="IMPORTED", komga_id=books[record["path"]])

    async def run(self):
        self.store.recover()
        while True:
            if not await self.run_once():
                await asyncio.sleep(1)
