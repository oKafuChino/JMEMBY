import asyncio
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile

from PIL import Image

from jmemby.config import Settings
from jmemby.db.store import Store
from jmemby.domain.errors import JobError
from jmemby.domain.models import Chapter, Manga, Page
from jmemby.providers.command import CommandProvider
from jmemby.providers.reference import parse_reference
from jmemby.runtime import backup_database, instance_lock
from jmemby.storage.cbz import contained, create_cbz, safe_component, validate_cbz
from jmemby.workers.queue import Worker


def png():
    stream = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(stream, "PNG")
    return stream.getvalue()


def manga():
    return Manga("jmcomic", "42", "A & <B>", ("Writer",), (Chapter("52", 1, "Chapter", ()),))


class FakeProvider:
    def __init__(self):
        self.downloads = 0
        self.block = False
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()

    async def get_pages(self, chapter_id):
        return (Page(1, "fixture:1"), Page(2, "fixture:2"))

    async def download(self, page, destination, max_bytes):
        self.downloads += 1
        self.started.set()
        try:
            if self.block:
                await asyncio.sleep(60)
            destination.write_bytes(png())
        finally:
            self.stopped.set()


class FakeKomga:
    def __init__(self):
        self.fail = False
        self.calls = 0

    async def import_paths(self, paths):
        self.calls += 1
        if self.fail:
            raise JobError("NETWORK", "暂时不可用", True)
        return {p: str(i) for i, p in enumerate(paths)}


class CoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_tags_survive_snapshot_and_packaging(self):
        from xml.etree import ElementTree
        from jmemby.db.store import decode_manga
        item = replace(manga(), tags=("冒险", "A & <B>", "冒险", "\x00"))
        task = self.store.submit(item, 1, 1)
        self.assertEqual(decode_manga(task["snapshot"]).tags, ("冒险", "A & <B>"))
        await self.worker.run_once()
        record = self.store.chapters(task["id"])[0]
        self.assertTrue(record["path"].endswith(" [JM42].cbz"))
        with zipfile.ZipFile(self.worker.library / record["path"]) as archive:
            info = ElementTree.fromstring(archive.read("ComicInfo.xml"))
            self.assertEqual(info.findtext("Tags"), "冒险, A & <B>")
            self.assertEqual(info.findtext("Series"), "A & <B> [JM42]")
            self.assertEqual(info.findtext("Title"), "Chapter [JM42]")

    def test_display_id_is_not_duplicated_or_added_to_other_sources(self):
        from xml.etree import ElementTree
        from jmemby.storage.cbz import comic_info
        item = replace(manga(), title="作品 [JM42]")
        chapter = replace(item.chapters[0], title="章节 [JM42]")
        info = ElementTree.fromstring(comic_info(item, chapter))
        self.assertEqual(info.findtext("Series"), item.title)
        self.assertEqual(info.findtext("Title"), chapter.title)
        item = replace(manga(), source="other")
        info = ElementTree.fromstring(comic_info(item, item.chapters[0]))
        self.assertEqual(info.findtext("Series"), item.title)
        self.assertEqual(info.findtext("Title"), item.chapters[0].title)

    def test_old_snapshots_and_existing_paths_remain_compatible(self):
        from dataclasses import asdict
        from jmemby.db.store import decode_manga
        raw = asdict(manga())
        raw.pop("tags")
        self.assertEqual(decode_manga(raw).tags, ())
        task = self.submit()
        record = self.store.chapters(task["id"])[0]
        self.store.connection.execute("UPDATE chapters SET path='legacy.cbz' WHERE id=?", (record["id"],))
        self.store.finish(task["id"], "FAILED", "NETWORK", "retry")
        new_task = self.submit()
        self.assertEqual(self.store.chapters(new_task["id"])[0]["path"], "legacy.cbz")

    async def test_sdk_scratch_file_does_not_poison_resume(self):
        task = self.submit()
        record = self.store.chapters(task["id"])[0]
        stage = self.worker.staging / task["id"] / str(record["id"])
        stage.mkdir(parents=True)
        (stage / "000001.decoded.jpg").write_bytes(b"interrupted SDK output")
        await self.worker.run_once()
        self.assertEqual(self.store.get(task["id"])["status"], "COMPLETED")

    def test_long_unicode_names_fit_linux_component_limit(self):
        from jmemby.storage.cbz import library_directory, chapter_filename
        item = replace(manga(), title="漫" * 300, source_id="章" * 100)
        chapter = replace(item.chapters[0], title="漫" * 300, source_id="章" * 100)
        for name in (library_directory(Path(), item).name, chapter_filename(chapter)):
            self.assertLessEqual(len(name.encode("utf-8")), 255)

    def test_cancel_waiting_task_finishes_without_worker(self):
        task = self.submit()
        self.store.request_cancel(task["id"], 1)
        self.assertEqual(self.store.get(task["id"])["status"], "CANCELLED")
        self.assertEqual(len(self.store.notifications()), 1)

    def test_cross_user_submission_does_not_return_another_users_task(self):
        self.submit()
        with self.assertRaises(ValueError):
            self.store.submit(manga(), 2, 2)

    def test_packaging_honors_configured_pixel_limit(self):
        stage = self.root / "limited-stage"
        stage.mkdir()
        (stage / "000001.png").write_bytes(png())
        chapter = replace(manga().chapters[0], pages=(Page(1, "fixture"),))
        target = self.root / "limited.cbz"
        with self.assertRaises(JobError):
            create_cbz(stage, target, manga(), chapter, max_pixels=1)
        self.assertFalse(target.exists())

    async def test_source_child_does_not_receive_other_service_secrets(self):
        code = "import os,json; print(json.dumps({k:os.getenv(k) for k in ['TELEGRAM_BOT_TOKEN','KOMGA_PASSWORD','JM_API_DOMAINS']}))"
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "secret", "KOMGA_PASSWORD": "secret", "JM_API_DOMAINS": "test"}):
            result = await CommandProvider(json.dumps([sys.executable, "-c", code])).request({})
        self.assertIsNone(result["TELEGRAM_BOT_TOKEN"])
        self.assertIsNone(result["KOMGA_PASSWORD"])
        self.assertEqual(result["JM_API_DOMAINS"], "test")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "app.db")
        self.provider = FakeProvider()
        self.komga = FakeKomga()
        self.settings = Settings("secret", frozenset({1}), "", str(self.root / "storage"), "", "", "", "/data",
                                 min_free_space_gb=0, resume_free_space_gb=1)
        self.worker = Worker(self.store, self.provider, self.komga, self.settings)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def submit(self):
        return self.store.submit(manga(), 1, 1)

    async def test_end_to_end_generated_images_and_duplicate_submission(self):
        task = self.submit()
        self.assertEqual(task["id"], self.submit()["id"])
        await self.worker.run_once()
        self.assertEqual(self.store.get(task["id"])["status"], "COMPLETED")
        record = self.store.chapters(task["id"])[0]
        path = self.worker.library / record["path"]
        with zipfile.ZipFile(path) as archive:
            self.assertEqual(archive.namelist()[:2], ["000001.png", "000002.png"])
            self.assertIn("A &amp; &lt;B&gt;", archive.read("ComicInfo.xml").decode())
        second = self.submit()
        await self.worker.run_once()
        self.assertEqual(self.provider.downloads, 2)
        self.assertEqual(self.store.get(second["id"])["status"], "COMPLETED")
        self.assertEqual(len(self.store.notifications()), 2)

    async def test_import_failure_reuses_artifact(self):
        task = self.submit()
        self.komga.fail = True
        await self.worker.run_once()
        self.assertEqual(self.store.get(task["id"])["status"], "RETRY_WAIT")
        self.assertIn("文件已保存", self.store.get(task["id"])["error"])
        self.komga.fail = False
        self.store.update(task["id"], next_at=0)
        await self.worker.run_once()
        self.assertEqual(self.provider.downloads, 2)
        self.assertEqual(self.store.get(task["id"])["status"], "COMPLETED")

    async def test_cancel_stops_running_operation(self):
        task = self.submit()
        self.provider.block = True
        running = asyncio.create_task(self.worker.run_once())
        await self.provider.started.wait()
        self.store.request_cancel(task["id"], 1)
        await asyncio.wait_for(running, 3)
        self.assertTrue(self.provider.stopped.is_set())
        self.assertEqual(self.store.get(task["id"])["status"], "CANCELLED")
        self.assertFalse(list(self.worker.library.rglob("*.cbz")))

    async def test_queued_cancel_does_not_download(self):
        task = self.submit()
        self.store.request_cancel(task["id"], 1)
        await self.worker.run_once()
        self.assertEqual(self.provider.downloads, 0)
        self.assertEqual(self.store.get(task["id"])["status"], "CANCELLED")

    async def test_recovery_after_publication_before_db_commit(self):
        task = self.submit()
        record = self.store.chapters(task["id"])[0]
        pages = await self.provider.get_pages("52")
        chapter = replace(manga().chapters[0], pages=pages)
        from dataclasses import asdict
        self.store.update_chapter(record["id"], pages=json.dumps([asdict(p) for p in pages]))
        staging = self.root / "stage"
        staging.mkdir()
        for p in pages:
            (staging / f"{p.number:06d}.png").write_bytes(png())
        create_cbz(staging, self.worker.library / record["path"], manga(), chapter)
        self.store.update(task["id"], status="PACKAGING")
        self.store.recover()
        await self.worker.run_once()
        self.assertEqual(self.provider.downloads, 0)
        self.assertEqual(self.store.get(task["id"])["status"], "COMPLETED")

    async def test_disk_pause_and_resume(self):
        task = self.submit()
        self.worker.settings = replace(self.settings, min_free_space_gb=1, resume_free_space_gb=2, disk_space_check_enabled=True)
        usage = type("Usage", (), {"free": 0})()
        with patch("jmemby.workers.queue.shutil.disk_usage", return_value=usage):
            await self.worker.run_once()
        self.assertEqual(self.store.get(task["id"])["status"], "PAUSED_DISK")
        usage.free = 3 * 1024**3
        with patch("jmemby.workers.queue.shutil.disk_usage", return_value=usage):
            await self.worker.run_once()
        self.assertEqual(self.store.get(task["id"])["status"], "COMPLETED")

    async def test_corrupt_existing_file_never_overwritten(self):
        task = self.submit()
        record = self.store.chapters(task["id"])[0]
        target = self.worker.library / record["path"]
        target.parent.mkdir(parents=True)
        target.write_bytes(b"user data")
        await self.worker.run_once()
        self.assertEqual(target.read_bytes(), b"user data")
        self.assertEqual(self.store.get(task["id"])["status"], "FAILED")

    async def test_confirmation_binding_expiry_and_idempotence(self):
        token = self.store.confirmation(manga(), 1, 1)
        with self.assertRaises(ValueError):
            self.store.confirm(token, 2)
        first = self.store.confirm(token, 1)
        self.assertEqual(first["id"], self.store.confirm(token, 1)["id"])
        other = self.store.confirmation(manga(), 1, 1)
        self.store.connection.execute("UPDATE confirmations SET expires=0 WHERE token=?", (other,))
        with self.assertRaises(ValueError):
            self.store.confirm(other, 1)

    async def test_retry_limit_and_atomic_notification(self):
        task = self.submit()
        self.komga.fail = True
        for _ in range(3):
            self.store.update(task["id"], next_at=0)
            await self.worker.run_once()
        self.assertEqual(self.store.get(task["id"])["status"], "FAILED")
        self.assertEqual(len(self.store.notifications()), 1)
        self.assertEqual(self.provider.downloads, 2)

    def test_database_unique_constraint_across_connections(self):
        first = self.submit()
        second = Store(self.root / "app.db")
        try:
            self.assertEqual(second.submit(manga(), 1, 1)["id"], first["id"])
        finally:
            second.close()

    def test_backup_and_future_schema(self):
        self.submit()
        destination = self.root / "backup.db"
        backup_database(self.root / "app.db", destination)
        restored = Store(destination)
        self.assertEqual(len(restored.tasks()), 1)
        restored.close()
        with sqlite3.connect(destination) as db:
            db.execute("PRAGMA user_version=99")
        db.close()
        with self.assertRaises(RuntimeError):
            Store(destination)

    def test_paths_and_references(self):
        for value in ("https://evil.test/album/1", "http://source.test/album/1", "abc", "0"):
            with self.assertRaises(ValueError):
                parse_reference(value, ("source.test",))
        self.assertEqual(parse_reference("https://source.test/album/42", ("source.test",)), "42")
        with self.assertRaises(JobError):
            contained(self.root, "../escape")
        self.assertNotEqual(safe_component(".."), "..")
        self.assertEqual(safe_component("CON"), "_CON")

    def test_single_instance_lock(self):
        with instance_lock(self.root / "lock"):
            with self.assertRaises(RuntimeError):
                with instance_lock(self.root / "lock"):
                    pass

    async def test_bad_image_is_not_published(self):
        task = self.submit()
        async def bad_download(page, destination, max_bytes):
            destination.write_bytes(b"not an image")
        self.provider.download = bad_download
        await self.worker.run_once()
        self.assertEqual(self.store.get(task["id"])["status"], "RETRY_WAIT")
        self.assertFalse(list(self.worker.library.rglob("*.cbz")))

    async def test_task_size_limit(self):
        task = self.submit()
        self.worker.settings = replace(self.settings, max_task_bytes=1)
        await self.worker.run_once()
        self.assertEqual(self.store.get(task["id"])["status"], "FAILED")
        self.assertFalse(list(self.worker.library.rglob("*.cbz")))

    async def test_batch_retry_requests_only_missing_pages(self):
        calls = []
        async def batch(pages, stage, max_bytes, max_pixels, budget):
            calls.append([p.number for p in pages])
            for page in pages:
                if len(calls) == 1 and page.number == 2:
                    raise JobError("NETWORK", "offline", True)
                (stage / f"{page.number:06d}.png").write_bytes(png())
        self.provider.download_batch = batch
        task = self.submit()
        await self.worker.run_once()
        self.assertEqual(self.store.get(task["id"])["status"], "RETRY_WAIT")
        self.store.update(task["id"], next_at=0)
        await self.worker.run_once()
        self.assertEqual(calls, [[1, 2], [2]])
        self.assertEqual(self.provider.downloads, 0)
        self.assertEqual(self.store.get(task["id"])["status"], "COMPLETED")

    async def test_disabled_disk_check_resumes_paused_task_without_reading_free_space(self):
        task = self.submit()
        self.store.update(task["id"], status="PAUSED_DISK")
        with patch("jmemby.workers.queue.shutil.disk_usage", side_effect=AssertionError("must not check")):
            await self.worker.run_once()
        self.assertEqual(self.store.get(task["id"])["status"], "COMPLETED")

    async def test_actual_disk_full_fails_without_auto_retry_when_checks_disabled(self):
        import errno
        task = self.submit()
        async def full(page, destination, max_bytes):
            raise OSError(errno.ENOSPC, "full")
        self.provider.download = full
        await self.worker.run_once()
        self.assertEqual(self.store.get(task["id"])["error_code"], "DISK_FULL")
        self.assertEqual(self.store.get(task["id"])["status"], "FAILED")
        self.assertFalse(await self.worker.run_once())

    def test_config_redacts_secret_and_rejects_empty_whitelist(self):
        self.assertNotIn("secret", repr(self.settings))
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "secret", "TELEGRAM_ALLOWED_USER_IDS": ""}):
            with self.assertRaises(ValueError):
                Settings.from_env()

    async def test_process_bridge_cancel_and_protocol(self):
        bridge = CommandProvider(json.dumps([sys.executable, "-c", "import json,sys; json.load(sys.stdin); print('{}')"]))
        self.assertEqual(await bridge.request({"operation": "ping"}), {})
        slow = CommandProvider(json.dumps([sys.executable, "-c", "import time; time.sleep(60)"]))
        task = asyncio.create_task(slow.request({}))
        await asyncio.sleep(.2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)


if __name__ == "__main__":
    unittest.main()
