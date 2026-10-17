import asyncio
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from jmemby.db.store import Store, SCHEMA
from jmemby.domain.discovery import DiscoveryDetail, evaluate
from jmemby.domain.errors import JobError
from jmemby.domain.models import Manga, Chapter
from jmemby.services.discovery import DiscoveryService
from jmemby.services.schedule_time import next_time, latest_due
from jmemby.services.schedules import ScheduleService
from jmemby.services.tasks import TaskService
from jmemby.services.users import UserService
from jmemby.workers.scheduler import Scheduler
from jmemby.workers.queue import Worker
from jmemby.providers.command import CommandProvider
from jmemby.providers.jm_adapter import handle
from test_core import FakeProvider, FakeKomga
from jmemby.config import Settings


def timestamp(value):
    return datetime.fromisoformat(value).timestamp()


def detail(source_id="1", tags=("冒险",), title="Example", likes=10, complete=True):
    return DiscoveryDetail(Manga("jmcomic", source_id, title, ("Writer",),
                                 (Chapter(source_id + "0", 1, "Chapter", ()),)),
                           tags, (), complete, False, str(likes), likes)


class Provider(FakeProvider):
    source = "jmcomic"

    def __init__(self):
        super().__init__()
        self.pages = [["1", "2", "3"]]
        self.details = {str(i): detail(str(i), likes=100 - i) for i in range(1, 5)}
        self.mode = "SOURCE_LIKES_DESC"
        self.searches = 0
        self.fail = None

    async def search_tag(self, tag, page):
        self.searches += 1
        if self.fail:
            raise self.fail
        return {"ids": self.pages[page - 1], "last": page >= len(self.pages),
                "mode": self.mode, "scope": "all"}

    async def get_discovery_detail(self, source_id):
        return self.details[source_id]


class PolicyTests(unittest.TestCase):
    def test_explicit_ai_variants(self):
        for marker in ("ai-generate", "AI GENERATED", "ＡＩ－ｇｅｎｅｒａｔｅｄ", "ai_generated", "AI生成", "ai–generated"):
            with self.subTest(marker=marker):
                self.assertEqual(evaluate(detail(tags=("冒险", marker)), "冒险"), ("BLOCK", "AI_TAG"))

    def test_unknown_is_not_empty_and_target_is_exact(self):
        self.assertEqual(evaluate(detail(complete=False), "冒险")[0], "UNKNOWN")
        self.assertEqual(evaluate(detail(tags=()), "冒险")[1], "TARGET_TAG_UNCONFIRMED")
        self.assertEqual(evaluate(detail(tags=("大冒险",)), "冒险")[0], "UNKNOWN")

    def test_titles_flags_and_false_positives(self):
        self.assertEqual(evaluate(detail(title="[AI-generated] Example"), "冒险")[0], "BLOCK")
        self.assertEqual(evaluate(replace(detail(), ai_flag=True), "冒险")[1], "AI_FLAG")
        self.assertEqual(evaluate(detail(title="Sailor Aiko"), "冒险")[0], "ALLOW")
        self.assertEqual(evaluate(detail(tags=("Sci Fi",)), " sci   fi ")[0], "ALLOW")

    def test_markers_do_not_count_as_target_tag(self):
        self.assertEqual(evaluate(replace(detail(tags=()), markers=("冒险",)), "冒险")[0], "UNKNOWN")


class TimeTests(unittest.TestCase):
    def test_daily_calendar_and_weekly(self):
        spec = {"kind": "daily", "time": "03:00", "timezone": "Asia/Hong_Kong"}
        now = timestamp("2026-10-18T18:00:00+00:00")
        self.assertEqual(next_time(spec, now), timestamp("2026-10-18T19:00:00+00:00"))
        weekly = dict(spec, kind="weekly", weekday=1)
        self.assertEqual(next_time(weekly, now), timestamp("2026-10-18T19:00:00+00:00"))

    def test_dst_gap_skipped_and_fold_runs_once(self):
        spec = {"kind": "daily", "time": "02:30", "timezone": "America/New_York"}
        self.assertEqual(next_time(spec, timestamp("2026-03-08T00:00:00-05:00")), timestamp("2026-03-09T02:30:00-04:00"))
        spec["time"] = "01:30"
        first = timestamp("2026-11-01T01:30:00-04:00")
        self.assertEqual(next_time(spec, first), timestamp("2026-11-02T01:30:00-05:00"))
        self.assertEqual(latest_due(spec, timestamp("2026-11-01T01:40:00-05:00")), first)

    def test_once_past_invalid_zone_and_clock_boundary(self):
        spec = {"kind": "once", "date": "2026-12-01", "time": "03:00", "timezone": "Asia/Hong_Kong"}
        at = timestamp("2026-12-01T03:00:00+08:00")
        self.assertEqual(next_time(spec, at - 1), at)
        self.assertIsNone(next_time(spec, at))
        with self.assertRaises(ValueError):
            next_time(dict(spec, timezone="Not/AZone"), at)


class ScheduleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.store = Store(self.path / "db.sqlite")
        self.users = UserService(self.store, {1})
        self.provider = Provider()
        self.now = timestamp("2026-10-18T02:00:00+08:00")
        self.service = ScheduleService(self.store, self.users, self.provider, self.path, lambda: self.now)
        self.scheduler = Scheduler(self.service)
        self.spec = {"kind": "daily", "time": "03:00", "timezone": "Asia/Hong_Kong"}

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def create(self, limit=2):
        return self.service.create(1, "冒险", self.spec, limit)

    def run_record(self, run_id):
        return dict(self.store.connection.execute("SELECT * FROM schedule_runs WHERE id=?", (run_id,)).fetchone())

    def start(self, schedule):
        token = self.service.action(schedule["id"], 1, "run")
        return self.service.confirm_action(token, 1)

    async def execute(self, limit=2):
        run_id = self.start(self.create(limit))
        await self.scheduler.process(self.run_record(run_id))
        return run_id

    def test_create_paused_permissions_and_action_identity(self):
        s = self.create()
        self.assertEqual(s["enabled"], 0)
        with self.assertRaises(ValueError):
            self.service.create(2, "冒险", self.spec)
        self.users.admin_ids = frozenset({1, 2})
        with self.assertRaises(ValueError):
            self.service.owned(s["id"], 2)
        token = self.service.action(s["id"], 1, "run")
        with self.assertRaises(ValueError):
            self.service.confirm_action(token, 2)
        run_id = self.service.confirm_action(token, 1)
        self.assertEqual(self.service.confirm_action(token, 1), run_id)

    def test_enable_confirmation_stale_and_expired(self):
        s = self.create()
        token = self.service.action(s["id"], 1, "enable")
        self.service.pause(s["id"], 1)
        with self.assertRaises(ValueError):
            self.service.confirm_action(token, 1)
        token = self.service.action(s["id"], 1, "enable")
        self.now += 601
        with self.assertRaises(ValueError):
            self.service.confirm_action(token, 1)

    def test_due_idempotence_misfire_and_overlap(self):
        s = self.create()
        self.service.confirm_action(self.service.action(s["id"], 1, "enable"), 1)
        self.now = timestamp("2026-10-21T03:10:00+08:00")
        self.service.claim_due()
        self.service.claim_due()
        rows = self.store.connection.execute("SELECT * FROM schedule_runs").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "DISCOVERING")
        self.assertIn("合并跳过", rows[0]["error"])
        self.now += 86400
        self.service.claim_due()
        self.assertEqual(self.store.connection.execute("SELECT status FROM schedule_runs ORDER BY created_at DESC LIMIT 1").fetchone()[0], "OVERLAP_SKIPPED")

    def test_missed_once_and_clock_rollback(self):
        self.spec.update(kind="once", date="2026-10-18")
        s = self.create()
        self.service.confirm_action(self.service.action(s["id"], 1, "enable"), 1)
        self.now += 5401
        self.service.claim_due()
        self.assertEqual(self.store.connection.execute("SELECT status FROM schedule_runs").fetchone()[0], "MISSED")
        self.now -= 4000
        self.service.claim_due()
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM schedule_runs").fetchone()[0], 1)

    async def test_high_likes_ai_excluded_and_stable_queue(self):
        self.provider.details["1"] = detail("1", tags=("冒险", "ai-generate"))
        run_id = await self.execute()
        tasks = self.store.connection.execute("SELECT * FROM tasks ORDER BY queue_sequence").fetchall()
        self.assertEqual([json.loads(t["snapshot"])["source_id"] for t in tasks], ["2", "3"])
        self.assertEqual(self.run_record(run_id)["status"], "QUEUED")
        self.assertEqual(self.store.connection.execute("SELECT reason FROM schedule_run_items WHERE source_id='1'").fetchone()[0], "AI_TAG")
        await self.scheduler.process(self.run_record(run_id))
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 2)

    async def test_preview_no_tasks_or_claims(self):
        result = await DiscoveryService(self.provider, self.service.skip).collect("冒险", 2)
        self.assertEqual(result["mode"], "SOURCE_LIKES_DESC")
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM auto_download_claims").fetchone()[0], 0)

    async def test_exact_sort_unknown_and_abbreviated_likes(self):
        self.provider.mode = "EXACT_LIKES_DESC"
        self.provider.details["1"] = replace(detail("1"), likes_raw="1K", likes_exact=None)
        self.provider.details["2"] = detail("2", likes=5)
        self.provider.details["3"] = detail("3", likes=100)
        await self.execute()
        tasks = self.store.connection.execute("SELECT snapshot FROM tasks ORDER BY queue_sequence").fetchall()
        self.assertEqual([json.loads(t[0])["source_id"] for t in tasks], ["3", "2"])

    async def test_unsupported_sort_fails_closed(self):
        self.provider.mode = "VIEWS_DESC"
        run_id = await self.execute()
        self.assertEqual(self.run_record(run_id)["status"], "FAILED")
        self.assertEqual(self.run_record(run_id)["error"], "DISCOVERY_UNSUPPORTED")

    async def test_duplicate_pages_are_bounded(self):
        self.provider.pages = [["1", "1"], ["1"]]
        run_id = await self.execute()
        self.assertEqual(self.provider.searches, 2)
        self.assertTrue(json.loads(self.run_record(run_id)["stats"])["truncated"])
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 1)

    async def test_unknown_detail_skipped_and_reserve_fills(self):
        self.provider.details["1"] = detail("1", complete=False)
        run_id = await self.execute()
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 2)
        self.assertIn("元数据不完整", self.service.summary(run_id))

    async def test_pause_during_discovery_prevents_admission(self):
        s = self.create()
        run_id = self.start(s)
        original = self.provider.search_tag

        async def search(tag, page):
            result = await original(tag, page)
            self.service.pause(s["id"], 1)
            return result

        self.provider.search_tag = search
        await self.scheduler.process(self.run_record(run_id))
        self.assertEqual(self.run_record(run_id)["status"], "CANCELLED")
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)

    async def test_cross_plan_claims_and_restart(self):
        first = await self.execute()
        for t in self.store.tasks():
            self.store.finish(t["id"], "FAILED")
        self.service.reconcile()
        other = await self.execute()
        self.assertNotEqual(first, other)
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 3)
        self.store.close()
        self.store = Store(self.path / "db.sqlite")
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM auto_download_claims").fetchone()[0], 3)

    async def test_manual_queue_precedes_batch_and_other_user_active_hidden(self):
        self.store.submit(detail("1").manga, 2, 2)
        await self.execute()
        tasks = self.store.connection.execute("SELECT * FROM tasks ORDER BY queue_sequence").fetchall()
        self.assertEqual(tasks[0]["user_id"], 2)
        self.assertEqual(len(tasks), 3)
        self.assertEqual(self.store.next_task()["id"], tasks[0]["id"])

    async def test_admission_transaction_rolls_back_and_recovers_planned(self):
        s = self.create()
        run_id = self.start(s)
        run = self.run_record(run_id)
        result = await DiscoveryService(self.provider, self.service.skip).collect("冒险", 2)
        self.service.save_plan(run, result)
        submit = self.store._submit
        calls = 0

        def crash(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("crash")
            return submit(*args)

        with patch.object(self.store, "_submit", side_effect=crash):
            with self.assertRaises(RuntimeError):
                self.service.admit(self.run_record(run_id))
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM auto_download_claims").fetchone()[0], 0)
        self.service.admit(self.run_record(run_id))
        self.service.admit(self.run_record(run_id))
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 2)

    async def test_download_recheck_blocks_images_and_retry_keeps_policy(self):
        run_id = await self.execute(limit=1)
        self.provider.details["1"] = detail("1", tags=("冒险", "AI生成"))
        settings = Settings("secret", frozenset({1}), "sqlite+aiosqlite:///unused", str(self.path), "http://komga", "", "lib", "/data")
        worker = Worker(self.store, self.provider, FakeKomga(), settings)
        await worker.run_once()
        self.assertEqual(self.provider.downloads, 0)
        task = self.store.tasks()[0]
        self.assertEqual(task["error_code"], "CONTENT_EXCLUDED")
        self.service.reconcile()
        retried = TaskService(self.store, self.provider, users=self.users).retry(task["id"], 1)
        self.assertEqual(retried["origin"], "schedule")
        self.assertEqual(retried["schedule_run_id"], run_id)
        self.assertEqual(self.run_record(run_id)["status"], "QUEUED")
        await worker.run_once()
        self.assertEqual(self.provider.downloads, 0)

    async def test_full_pipeline_then_skip_imported_and_finish_summary(self):
        run_id = await self.execute(limit=1)
        settings = Settings("secret", frozenset({1}), "sqlite+aiosqlite:///unused", str(self.path), "http://komga", "", "lib", "/data")
        await Worker(self.store, self.provider, FakeKomga(), settings).run_once()
        self.service.reconcile()
        self.assertEqual(self.run_record(run_id)["status"], "FINISHED")
        self.assertEqual(self.service.skip("jmcomic", "1"), "AUTO_ALREADY_ATTEMPTED")
        self.store.connection.execute("DELETE FROM auto_download_claims")
        self.assertEqual(self.service.skip("jmcomic", "1"), "ALREADY_IMPORTED")
        target = self.path / "library" / self.store.chapters(self.store.tasks()[0]["id"])[0]["path"]
        target.unlink()
        self.assertEqual(self.service.skip("jmcomic", "1"), "NEEDS_REPAIR")

    async def test_pause_does_not_cancel_queued_but_cancel_run_does(self):
        run_id = await self.execute()
        s = self.run_record(run_id)["schedule_id"]
        self.service.pause(s, 1)
        self.assertTrue(all(t["status"] == "QUEUED" for t in self.store.tasks()))
        token = self.service.action(s, 1, "cancel:" + run_id)
        self.service.confirm_action(token, 1)
        self.service.confirm_action(token, 1)
        self.assertTrue(all(t["status"] == "CANCELLED" for t in self.store.tasks()))

    async def test_retry_persistent_delay_and_bounded_attempts(self):
        self.provider.fail = JobError("RATE_LIMIT", "wait", True, 60)
        run_id = await self.execute()
        r = self.run_record(run_id)
        self.assertEqual(r["attempts"], 1)
        self.assertEqual(r["next_attempt_at"], self.now + 60)
        self.now += 60
        await self.scheduler.process(self.run_record(run_id))
        self.now += 60
        await self.scheduler.process(self.run_record(run_id))
        self.assertEqual(self.run_record(run_id)["status"], "FAILED")

    async def test_removed_admin_and_budget_no_search(self):
        run_id = self.start(self.create())
        self.users.admin_ids = frozenset()
        await self.scheduler.process(self.run_record(run_id))
        self.assertEqual(self.run_record(run_id)["status"], "CANCELLED")
        self.assertEqual(self.provider.searches, 0)
        self.users.admin_ids = frozenset({1})
        run_id = self.start(self.create())
        self.now += 601
        await self.scheduler.process(self.run_record(run_id))
        self.assertEqual(self.run_record(run_id)["error"], "DISCOVERY_BUDGET")

    async def test_backpressure_waits_without_source_and_keeps_attempt_budget(self):
        run_id = self.start(self.create())
        with patch.object(self.service, "pending_count", return_value=50):
            for _ in range(5):
                await self.scheduler.process(self.run_record(run_id))
                self.now += 15
        self.assertEqual(self.provider.searches, 0)
        self.assertEqual(self.run_record(run_id)["attempts"], 0)
        await self.scheduler.process(self.run_record(run_id))
        self.assertEqual(self.run_record(run_id)["status"], "QUEUED")

    async def test_cancelling_run_survives_restart_until_worker_stops(self):
        run_id = await self.execute(limit=1)
        task = self.store.next_task()
        self.service.cancel_run(run_id, 1)
        self.assertEqual(self.run_record(run_id)["status"], "CANCELLING")
        self.assertTrue(self.service.has_active(self.run_record(run_id)["schedule_id"]))
        self.store.finish(task["id"], "CANCELLED")
        self.service.reconcile()
        self.assertEqual(self.run_record(run_id)["status"], "CANCELLED")

    async def test_schedule_retry_does_not_reuse_unfiltered_manual_task(self):
        await self.execute(limit=1)
        task = self.store.tasks()[0]
        self.store.finish(task["id"], "FAILED")
        self.store.submit(detail("1").manga, 1, 1)
        with self.assertRaises(ValueError):
            TaskService(self.store, self.provider, users=self.users).retry(task["id"], 1)

    def test_schema_four_migration_preserves_accounts_and_fifo(self):
        path = self.path / "legacy.sqlite"
        db = sqlite3.connect(path)
        db.executescript(SCHEMA + """
        CREATE TABLE users(user_id INTEGER PRIMARY KEY);
        CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE invites(id INTEGER PRIMARY KEY,code_hash TEXT);
        CREATE TABLE komga_accounts(user_id INTEGER PRIMARY KEY,email TEXT,password_encrypted TEXT);
        INSERT INTO users VALUES (7);
        INSERT INTO komga_accounts VALUES (7,'tg7@jmemby.local','encrypted');
        INSERT INTO manga VALUES (1,'jmcomic','42','Old','old','{}');
        INSERT INTO tasks(id,manga_id,snapshot,user_id,chat_id,created_at,updated_at,status)
         VALUES ('b',1,'{}',7,7,2,2,'COMPLETED'), ('a',1,'{}',7,7,1,1,'COMPLETED');
        PRAGMA user_version=4;
        """)
        db.close()
        upgraded = Store(path)
        try:
            self.assertEqual(upgraded.connection.execute("PRAGMA user_version").fetchone()[0], 5)
            self.assertEqual(upgraded.connection.execute("SELECT password_encrypted FROM komga_accounts").fetchone()[0], "encrypted")
            self.assertEqual([r[0] for r in upgraded.connection.execute("SELECT id FROM tasks ORDER BY queue_sequence")], ["a", "b"])
            self.assertTrue(all(r[0] == "manual" for r in upgraded.connection.execute("SELECT origin FROM tasks")))
        finally:
            upgraded.close()

    async def test_empty_result_and_notifications_are_idempotent(self):
        self.provider.pages = [[]]
        run_id = await self.execute()
        self.assertEqual(self.run_record(run_id)["status"], "EMPTY")
        self.service.reconcile()
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM schedule_notifications WHERE run_id=?", (run_id,)).fetchone()[0], 1)

    async def test_scheduler_loop_claims_due_and_shutdown_keeps_durable_tasks(self):
        s = self.create()
        self.service.confirm_action(self.service.action(s["id"], 1, "enable"), 1)
        self.now += 3600
        operation = asyncio.create_task(self.scheduler.run())
        async def wait_admission():
            while not self.store.tasks():
                await asyncio.sleep(0.01)
        try:
            await asyncio.wait_for(wait_admission(), timeout=2)
        finally:
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
        self.assertEqual(len(self.store.tasks()), 2)
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM schedule_runs").fetchone()[0], 1)

    async def test_scheduler_shutdown_cancels_inflight_discovery_for_restart(self):
        started, stopped = asyncio.Event(), asyncio.Event()
        async def blocked(tag, page):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        self.provider.search_tag = blocked
        run_id = self.start(self.create())
        operation = asyncio.create_task(self.scheduler.run())
        await asyncio.wait_for(started.wait(), timeout=2)
        operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)
        self.assertTrue(stopped.is_set())
        self.assertEqual(self.run_record(run_id)["status"], "DISCOVERING")
        self.provider.search_tag = Provider().search_tag
        await self.scheduler.process(self.run_record(run_id))
        self.assertEqual(self.run_record(run_id)["status"], "QUEUED")


class AdapterDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_protocol_missing_or_truncated_fields_fail_closed(self):
        class Provider(CommandProvider):
            async def request(self, req):
                return self.result
        p = Provider('["unused"]')
        p.result = {"manga": {"source": "jmcomic", "source_id": "1", "title": "Test", "authors": [],
                              "chapters": [{"source_id": "2", "ordinal": 1, "title": "One", "pages": []}]},
                    "complete": True, "markers": []}
        self.assertFalse((await p.get_discovery_detail("1")).complete)
        p.result["tags"] = ["a"] * 1001
        self.assertFalse((await p.get_discovery_detail("1")).complete)
        p.result["tags"] = []
        self.assertTrue((await p.get_discovery_detail("1")).complete)
        p.result = {"ids": ["1"], "last": True, "mode": "DEFAULT"}
        with self.assertRaises(JobError):
            await p.search_tag("a", 1)

    def test_sdk_strict_metadata_keeps_raw_tags_and_likes(self):
        class Album:
            name, authors, tags, works, characters, likes = "Test", [], ["冒险", "ai-generate"], [], [], "1K"
            def __iter__(self):
                return iter([type("Photo", (), {"id": "2", "name": "One"})()])
        class SDK:
            def get_album_detail(self, source_id):
                return Album()
        result = handle({"operation": "discovery_detail", "id": "1"}, SDK())
        self.assertTrue(result["complete"])
        self.assertIsNone(result["likes_exact"])
        self.assertIn("ai-generate", result["tags"])

    def test_sdk_requests_like_order_and_all_time_without_fallback(self):
        import sys
        from types import SimpleNamespace
        class SDK:
            def search_tag(self, **kwargs):
                self.kwargs = kwargs
                return SimpleNamespace(iter_id_title=lambda: iter([("1", "Test")]), page_count=1)
        sdk = SDK()
        constants = SimpleNamespace(ORDER_BY_LIKE="mr", TIME_ALL="a")
        with patch.dict(sys.modules, {"jmcomic": SimpleNamespace(JmMagicConstants=constants)}):
            result = handle({"operation": "search_tag", "tag": "冒险", "page": 1}, sdk)
        self.assertEqual(sdk.kwargs, {"search_query": "冒险", "page": 1, "order_by": "mr", "time": "a"})
        self.assertEqual(result["mode"], "SOURCE_LIKES_DESC")
        self.assertTrue(result["last"])


if __name__ == "__main__":
    unittest.main()
