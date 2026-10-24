from pathlib import Path
import tempfile
import unittest

from jmemby.db.store import Store
from jmemby.services.users import UserService
from jmemby.bot.access import chat_allows


class GrantTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'db.sqlite'
        self.store = Store(self.path)
        self.users = UserService(self.store, {1})
        self.users.set_invite_required(1, True)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_qualification_requires_registration_and_bypasses_only_invite(self):
        self.users.grant(1, 2)
        with self.assertRaises(ValueError):
            self.users.require_member(2)
        self.users.set_registration(1, False)
        with self.assertRaises(ValueError):
            self.users.register(2)
        self.users.set_registration(1, True)
        self.users.register(2)
        self.assertEqual(self.users.group_label(2), '普通')
        self.users.require_member(2)
        with self.assertRaises(ValueError):
            self.users.register(3)

    def test_white_persists_and_kk_never_downgrades(self):
        self.users.grant(1, 2, white=True)
        self.users.grant(1, 2)
        self.store.close()
        self.store = Store(self.path)
        self.users = UserService(self.store, {1})
        self.users.register(2)
        self.users.grant(1, 2)
        self.assertEqual(self.users.group_label(2), '白名单')
        self.users.require_member(2)
        with self.assertRaises(ValueError):
            self.users.require_admin(2)

    def test_existing_user_upgrade_does_not_unban(self):
        self.users.grant(1, 2)
        self.users.register(2)
        self.users.grant(1, 2, white=True)
        self.assertEqual(self.users.get(2)['user_group'], 'white')
        self.users.set_blocked(1, 2, True)
        for white in (True, False):
            with self.assertRaises(ValueError):
                self.users.grant(1, 2, white)
        with self.assertRaises(ValueError):
            self.users.require_member(2)

    def test_non_admin_cannot_grant_or_modify_admin(self):
        for actor, target in ((2, 3), (1, 1), (1, -100)):
            with self.assertRaises(ValueError):
                self.users.grant(actor, target, True)
        self.assertEqual(self.store.connection.execute('SELECT COUNT(*) FROM registration_grants').fetchone()[0], 0)

    def test_group_only_boundaries(self):
        for command in ('/kk', '/white'):
            self.assertFalse(chat_allows('private', 1, 1, command))
            self.assertFalse(chat_allows('channel', -100, 1, command))
            for kind in ('group', 'supergroup'):
                self.assertTrue(chat_allows(kind, -100, 1, command))

    def test_v6_upgrade_preserves_users_and_is_idempotent(self):
        self.users.set_invite_required(1, False)
        self.users.register(2)
        self.store.connection.executescript('ALTER TABLE tasks DROP COLUMN notify_telegram; DROP TABLE report_deliveries; DROP TABLE manga_reports; DROP TABLE manga_likes; ALTER TABLE manga DROP COLUMN removed_at; DROP TABLE download_nodes; ALTER TABLE users DROP COLUMN user_group; DROP TABLE registration_grants; DROP INDEX task_priority_sequence; ALTER TABLE tasks DROP COLUMN priority; PRAGMA user_version=6;')
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.connection.execute('PRAGMA user_version').fetchone()[0], 13)
        self.assertEqual(UserService(self.store, {1}).get(2)['user_group'], 'normal')
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.connection.execute('SELECT COUNT(*) FROM users').fetchone()[0], 1)
