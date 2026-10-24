"""Removal migration against the historical schema, with manual-flow regressions."""
from dataclasses import asdict, replace
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from jmemby.config import Settings
from jmemby.db.store import Store, TERMINAL
from jmemby.providers.command import CommandProvider
from jmemby.providers.jm_adapter import handle
from jmemby.services.tasks import TaskService
from jmemby.services.users import UserService
from jmemby.workers.queue import Worker
from test_core import FakeKomga, FakeProvider, manga

STATES = ("QUEUED", "DOWNLOADING", "PACKAGING", "IMPORT_PENDING", "RETRY_WAIT",
          "PAUSED_DISK", "CANCEL_REQUESTED", "COMPLETED", "FAILED", "CANCELLED")


class RemovalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "legacy.db"
        self.store = None
        db = sqlite3.connect(self.path)
        self.addCleanup(db.close)
        db.executescript((Path(__file__).parent / "fixtures/schema_v5.sql").read_text(encoding="utf-8"))
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("INSERT INTO users(user_id,username,display_name,registered_at) VALUES (1,'reader','Reader',1)")
        db.execute("INSERT INTO komga_accounts(user_id,email,password_encrypted,status,created_at) VALUES (1,'reader@example.test','ciphertext','READY',1)")
        db.execute("INSERT INTO invites(code_hash,max_uses,created_by,created_at) VALUES ('hash',3,1,1)")
        db.execute("INSERT INTO schedules VALUES (1,1,1,'tag','{}',50,1,1,1,1)")
        db.execute("INSERT INTO schedule_runs(id,schedule_id,scheduled_for,trigger_type,status,snapshot,created_at) VALUES ('run',1,1,'timer','QUEUED','{}',1)")
        self.original = {}
        number = 0
        for origin in ("manual", "schedule"):
            for state in STATES:
                number += 1
                item = replace(manga(), source_id=str(number))
                raw = json.dumps(asdict(item))
                task_id = f"{origin}_{state}"
                db.execute("INSERT INTO manga VALUES (?,?,?,?,?,?)", (number, item.source, item.source_id, item.title, f"legacy-{number}", raw))
                db.execute("INSERT INTO tasks(id,manga_id,snapshot,user_id,chat_id,status,origin,schedule_run_id,policy_version,queue_sequence,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                           (task_id, number, raw, 1, 1, state, origin, "run" if origin == "schedule" else None,
                            1 if origin == "schedule" else None, number, number, number))
                db.execute("INSERT INTO chapters(id,manga_id,source_id,ordinal,title,path,status,checksum) VALUES (?,?,?,?,?,?,?,?)",
                           (number, number, "52", 1, "Chapter", f"legacy-{number}/book.cbz", "IMPORTED" if state == "COMPLETED" else "MISSING", "preserve-checksum"))
                db.execute("INSERT INTO task_chapters VALUES (?,?,'PENDING')", (task_id, number))
                db.execute("INSERT INTO notifications(task_id,event) VALUES (?,?)", (task_id, state))
                if origin == "schedule":
                    db.execute("INSERT INTO schedule_run_items(run_id,source,source_id,rank,policy_version,decision,reason,task_id) VALUES ('run','jmcomic',?,?,1,'QUEUED','MATCH',?)", (item.source_id, number, task_id))
                    db.execute("INSERT INTO auto_download_claims VALUES ('jmcomic',?,'run',?)", (item.source_id, task_id))
                self.original[task_id] = (state, raw, number)
        db.execute("INSERT INTO schedule_actions VALUES ('token',1,1,'enable',99999,1,NULL)")
        db.execute("INSERT INTO schedule_drafts VALUES (1,'tag','{}',99999)")
        db.execute("INSERT INTO schedule_notifications(run_id,event,text) VALUES ('run','queued','old notice')")
        db.execute("INSERT INTO settings VALUES ('schedule_active_download','schedule_DOWNLOADING')")
        db.execute("INSERT INTO settings VALUES ('schedule_download_after','99999999999')")
        db.execute("INSERT INTO confirmations VALUES ('manual-token',1,1,?,99999999999,'manual_QUEUED',0)", (self.original['manual_QUEUED'][1],))
        db.commit()
        self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
        db.close()
        self.saved = self.root / "library/legacy-18/book.cbz"
        self.saved.parent.mkdir(parents=True)
        self.saved.write_bytes(b"existing published file - preserve verbatim")

    def tearDown(self):
        if self.store:
            self.store.close()
        self.temp.cleanup()

    def upgrade(self):
        self.store = Store(self.path)
        return self.store.connection

    def test_pending_auto_tasks_cancelled_manual_and_terminal_history_preserved(self):
        db = self.upgrade()
        self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 6)
        for task_id, (state, raw, sequence) in self.original.items():
            task = self.store.get(task_id)
            if task_id.startswith("schedule_") and state not in TERMINAL:
                self.assertEqual(task["status"], "CANCELLED")
                self.assertEqual(task["cancel_requested"], 1)
                self.assertEqual(task["error_code"], "AUTO_DOWNLOAD_REMOVED")
            else:
                self.assertEqual(task["status"], state)
            self.assertEqual(task["snapshot"], raw)
            self.assertEqual(task["queue_sequence"], sequence)
        self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_tables_fields_and_cooldown_removed_with_files_and_accounts_preserved(self):
        db = self.upgrade()
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertFalse(any(name.startswith('schedule') for name in tables))
        self.assertNotIn("auto_download_claims", tables)
        columns = {r[1] for r in db.execute("PRAGMA table_info(tasks)")}
        self.assertFalse(columns.intersection({"origin", "policy_version", "schedule_run_id"}))
        self.assertEqual(dict(db.execute("SELECT * FROM settings")), {"registration_open": "1", "invite_required": "0"})
        self.assertEqual(db.execute("SELECT password_encrypted FROM komga_accounts").fetchone()[0], "ciphertext")
        self.assertEqual(db.execute("SELECT code_hash FROM invites").fetchone()[0], "hash")
        self.assertEqual(db.execute("SELECT COUNT(*) FROM task_chapters").fetchone()[0], 20)
        self.assertEqual(db.execute("SELECT path,checksum FROM chapters WHERE id=18").fetchone()[:], ("legacy-18/book.cbz", "preserve-checksum"))
        self.assertEqual(self.saved.read_bytes(), b"existing published file - preserve verbatim")
        self.assertEqual(self.store.confirm("manual-token", 1)["id"], "manual_QUEUED")

    def test_old_auto_notices_are_suppressed_without_suppressing_manual_notices(self):
        db = self.upgrade()
        for row in db.execute("SELECT task_id,status FROM notifications"):
            self.assertEqual(row["status"], "SKIPPED" if row["task_id"].startswith("schedule_") else "PENDING")
        self.assertTrue(all(row["task_id"].startswith("manual_") for row in self.store.notifications()))

    def test_restart_is_idempotent_and_never_resumes_cancelled_auto_tasks(self):
        db = self.upgrade()
        before = [tuple(row) for row in db.execute("SELECT * FROM tasks ORDER BY queue_sequence")]
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(before, [tuple(row) for row in self.store.connection.execute("SELECT * FROM tasks ORDER BY queue_sequence")])
        self.store.recover()
        seen = []
        while task := self.store.next_task():
            seen.append(task["id"])
            self.store.finish(task["id"], "CANCELLED")
        self.assertTrue(seen)
        self.assertTrue(all(task_id.startswith("manual_") for task_id in seen))

    def test_failed_migration_rolls_back_all_changes_and_releases_lock(self):
        from jmemby.db.migrations import REMOVE_AUTOMATION_FROM_V5
        broken = REMOVE_AUTOMATION_FROM_V5.replace("PRAGMA user_version=6;", "SELECT nonexistent_column;")
        with patch("jmemby.db.migrations.REMOVE_AUTOMATION_FROM_V5", broken):
            with self.assertRaises(sqlite3.OperationalError):
                Store(self.path)
        db = sqlite3.connect(self.path, isolation_level=None, timeout=0.1)
        try:
            db.execute("BEGIN IMMEDIATE")
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 5)
            self.assertEqual(db.execute("SELECT status FROM tasks WHERE id='schedule_QUEUED'").fetchone()[0], "QUEUED")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM schedules").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM auto_download_claims").fetchone()[0], 10)
            self.assertEqual(db.execute("SELECT status FROM notifications WHERE task_id='schedule_QUEUED'").fetchone()[0], "PENDING")
            db.execute("ROLLBACK")
        finally:
            db.close()
        self.upgrade()

    def test_new_database_has_only_manual_queue_and_stable_fifo(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "new.db")
            try:
                self.assertEqual(store.connection.execute("PRAGMA user_version").fetchone()[0], 6)
                self.assertEqual(store.connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'schedule%'").fetchone()[0], 0)
                first = store.submit(manga(), 1, 1)
                second = store.submit(replace(manga(), source_id="43"), 1, 1)
                self.assertEqual(store.next_task()["id"], first["id"])
                store.finish(first["id"], "COMPLETED")
                self.assertEqual(store.next_task()["id"], second["id"])
            finally:
                store.close()

    async def test_manual_download_and_import_work_immediately_after_upgrade(self):
        db = self.upgrade()
        # Keep this fixture's remaining manual jobs out of the integration path.
        db.execute("UPDATE tasks SET status='CANCELLED' WHERE status NOT IN ('COMPLETED','FAILED','CANCELLED')")
        provider, komga = FakeProvider(), FakeKomga()
        settings = Settings("secret", frozenset({1}), "unused", str(self.root), "http://komga", "", "lib", "/data")
        worker = Worker(self.store, provider, komga, settings)
        first = self.store.submit(manga(), 1, 1)
        second = self.store.submit(replace(manga(), source_id="43"), 1, 1)
        self.assertTrue(await worker.run_once())
        self.assertTrue(await worker.run_once())
        self.assertEqual(self.store.get(first["id"])["status"], "COMPLETED")
        self.assertEqual(self.store.get(second["id"])["status"], "COMPLETED")
        self.assertEqual(provider.downloads, 4)
        self.assertEqual(komga.calls, 2)

    def test_version_four_upgrades_directly_to_manual_fifo(self):
        path = self.root / "v4.db"
        old = Store(path)
        try:
            with patch("jmemby.db.store.time.time", return_value=100):
                first = old.submit(manga(), 1, 1)
                second = old.submit(replace(manga(), source_id="43"), 1, 1)
            # Schema 4 had the same manual tables, without the FIFO extension.
            old.connection.execute("DROP INDEX task_sequence")
            old.connection.execute("ALTER TABLE tasks DROP COLUMN queue_sequence")
            old.connection.execute("PRAGMA user_version=4")
        finally:
            old.close()
        upgraded = Store(path)
        try:
            rows = upgraded.connection.execute("SELECT id,queue_sequence FROM tasks ORDER BY queue_sequence").fetchall()
            self.assertEqual([r[0] for r in rows], sorted([first["id"], second["id"]]))
            self.assertEqual([r[1] for r in rows], [1, 2])
            self.assertEqual(upgraded.connection.execute("PRAGMA user_version").fetchone()[0], 6)
            self.assertEqual(upgraded.connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        finally:
            upgraded.close()

    def test_explicit_retry_of_historical_failed_task_is_a_normal_manual_task(self):
        self.upgrade()
        service = TaskService(self.store, FakeProvider(), users=UserService(self.store, {1}))
        retried = service.retry("schedule_FAILED", 1)
        self.assertEqual(retried["status"], "QUEUED")
        self.assertEqual(retried["retry_of"], "schedule_FAILED")
        self.assertNotIn("origin", retried)

    def test_obsolete_source_operations_are_not_exposed(self):
        provider = CommandProvider('["unused"]')
        self.assertFalse(hasattr(provider, "search_tag"))
        self.assertFalse(hasattr(provider, "get_discovery_detail"))
        for operation in ("search_tag", "discovery_detail"):
            with self.assertRaisesRegex(ValueError, "Unsupported operation"):
                handle({"operation": operation, "id": "42", "tag": "tag", "page": 1}, object())

    def test_obsolete_timezone_does_not_affect_configuration(self):
        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "test", "TELEGRAM_ALLOWED_USER_IDS": "1",
                                       "SCHEDULE_DEFAULT_TIMEZONE": "not/a/timezone"}, clear=True):
            settings = Settings.from_env()
        self.assertFalse(hasattr(settings, "schedule_default_timezone"))


HAS_AIOGRAM = importlib.util.find_spec("aiogram") is not None
if HAS_AIOGRAM:
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from aiogram import Bot
    from aiogram.types import CallbackQuery, Chat, Message, Update, User
    from jmemby.bot.app import create_dispatcher


@unittest.skipUnless(HAS_AIOGRAM, "aiogram is required for real dispatcher tests")
class RemovedEntryTests(unittest.IsolatedAsyncioTestCase):
    async def test_menu_and_old_commands_and_buttons_cannot_create_downloads(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "db.sqlite")
            bot = Bot("123456:ABCdef123456")
            try:
                provider = SimpleNamespace(get_manga=AsyncMock())
                service = TaskService(store, provider, users=UserService(store, {1}))
                dispatcher = create_dispatcher(service, SimpleNamespace(), None, None)
                user = User(id=1, is_bot=False, first_name="Test")
                with patch.object(Message, "answer", new_callable=AsyncMock) as answer:
                    for index, text in enumerate(("/start", "/schedules", "/schedule_add", "⏰ 定时下载"), 1):
                        message = Message(message_id=index, date=datetime.now(timezone.utc), text=text,
                                          from_user=user, chat=Chat(id=1, type="private"))
                        await dispatcher.feed_update(bot, Update(update_id=index, message=message))
                        keyboard = answer.call_args.kwargs["reply_markup"]
                        self.assertFalse(any("定时" in button.text for row in keyboard.keyboard for button in row))
                        if index > 1:
                            self.assertIn("已移除", answer.call_args.args[0])
                with patch.object(CallbackQuery, "answer", new_callable=AsyncMock) as answer:
                    callback = CallbackQuery(id="old", from_user=user, chat_instance="test", data="sched:old-token", message=message)
                    await dispatcher.feed_update(bot, Update(update_id=5, callback_query=callback))
                    self.assertIn("已失效", answer.call_args.args[0])
                provider.get_manga.assert_not_awaited()
                self.assertEqual(store.tasks(), [])
            finally:
                await bot.session.close()
                store.close()


if __name__ == "__main__":
    unittest.main()
