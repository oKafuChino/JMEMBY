import asyncio
from pathlib import Path
import sqlite3
import tempfile
import unittest

from jmemby.db.store import SCHEMA, Store
from jmemby.domain.models import Chapter, Manga
from jmemby.services.tasks import TaskService
from jmemby.services.users import UserService


class UsersTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "app.db"
        self.store = Store(self.path)
        self.users = UserService(self.store, {1})
        self.manga = Manga("test", "42", "Example", (), (Chapter("52", 1, "One", ()),))

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def test_register_is_idempotent_and_persistent(self):
        self.users.register(2, "someone", "Name")
        self.users.register(2, "someone", "Name")
        self.assertEqual(self.users.statistics(1)["total"], 1)
        self.store.close()
        self.store = Store(self.path)
        self.users = UserService(self.store, {1})
        self.users.require_member(2)
        self.assertEqual(self.users.get(2)["username"], "someone")

    def test_registration_toggle_does_not_lock_existing_members(self):
        self.users.register(2)
        self.users.set_registration(1, False)
        self.users.require_member(2)
        self.users.require_member(1)
        self.users.register(2)
        with self.assertRaises(ValueError):
            self.users.register(3)
        self.users.set_registration(1, True)
        self.users.register(3)
        self.assertEqual(self.users.statistics(1)["total"], 2)

    def test_admin_operations_reject_registered_nonadmin(self):
        self.users.register(2)
        for action in (lambda: self.users.statistics(2), lambda: self.users.list_users(2),
                       lambda: self.users.set_registration(2, False),
                       lambda: self.users.set_blocked(2, 1, True)):
            with self.assertRaises(ValueError):
                action()
        self.assertTrue(self.users.registration_open())

    def test_admin_does_not_count_as_registered_member_or_allow_ban(self):
        self.users.register(1)
        self.assertEqual(self.users.statistics(1)["total"], 0)
        with self.assertRaises(ValueError):
            self.users.set_blocked(1, 1, True)

    def test_ban_invalidates_buttons_and_requests_task_cancellation(self):
        self.users.register(2)
        token = self.store.confirmation(self.manga, 2, 2)
        task = self.store.submit(self.manga, 2, 2)
        self.users.set_blocked(1, 2, True)
        self.assertTrue(self.store.get(task["id"])["cancel_requested"])
        with self.assertRaises(ValueError):
            self.users.register(2)
        with self.assertRaises(ValueError):
            self.users.require_member(2)
        self.users.set_blocked(1, 2, False)
        self.users.require_member(2)
        with self.assertRaises(ValueError):
            self.store.confirm(token, 2)
        self.assertTrue(self.store.get(task["id"])["cancel_requested"])

    def test_stats_and_pagination(self):
        for number in range(2, 15):
            self.users.register(number)
        self.users.set_blocked(1, 2, True)
        self.assertEqual(len(self.users.list_users(1, 1)), 10)
        self.assertEqual(len(self.users.list_users(1, 2)), 3)
        self.assertEqual(self.users.statistics(1)["active"], 12)
        self.assertEqual(self.users.statistics(1)["blocked"], 1)

    async def test_unregistered_user_never_calls_provider(self):
        class Provider:
            async def get_manga(self, source_id):
                raise AssertionError("must not reach provider")
        service = TaskService(self.store, Provider(), users=self.users)
        with self.assertRaises(ValueError):
            await service.preview("42", 2, 2)

    async def test_banned_during_preview_cannot_create_confirmation(self):
        self.users.register(2)
        outer = self
        class Provider:
            async def get_manga(self, source_id):
                outer.users.set_blocked(1, 2, True)
                return outer.manga
        service = TaskService(self.store, Provider(), users=self.users)
        with self.assertRaises(ValueError):
            await service.preview("42", 2, 2)
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM confirmations").fetchone()[0], 0)

    def test_registered_users_cannot_read_others_tasks(self):
        self.users.register(2)
        self.users.register(3)
        task = self.store.submit(self.manga, 2, 2)
        service = TaskService(self.store, None, users=self.users)
        with self.assertRaises(ValueError):
            service.get(task["id"], 3)

    def test_banned_member_cannot_confirm_or_retry(self):
        self.users.register(2)
        token = self.store.confirmation(self.manga, 2, 2)
        task = self.store.submit(self.manga, 2, 2)
        self.store.finish(task["id"], "FAILED")
        self.users.set_blocked(1, 2, True)
        service = TaskService(self.store, None, users=self.users)
        with self.assertRaises(ValueError):
            service.confirm(token, 2)
        with self.assertRaises(ValueError):
            service.retry(task["id"], 2)

    def test_registration_setting_survives_restart(self):
        self.users.set_registration(1, False)
        self.store.close()
        self.store = Store(self.path)
        self.assertFalse(UserService(self.store, {1}).registration_open())

    def test_version_one_migration_preserves_existing_tables(self):
        legacy = Path(self.temporary.name) / "legacy.db"
        db = sqlite3.connect(legacy)
        db.executescript(SCHEMA + "PRAGMA user_version=1;")
        db.execute("INSERT INTO manga(source,source_id,title,directory,metadata) VALUES ('jm','42','Old','old','{}')")
        db.commit()
        db.close()
        upgraded = Store(legacy)
        try:
            self.assertEqual(upgraded.connection.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(upgraded.connection.execute("SELECT title FROM manga").fetchone()[0], "Old")
            UserService(upgraded, {1}).register(2)
        finally:
            upgraded.close()


if __name__ == "__main__":
    unittest.main()
