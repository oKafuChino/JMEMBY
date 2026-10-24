from dataclasses import replace
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from jmemby.db.store import Store
from jmemby.domain.models import Manga, Chapter
from jmemby.services.users import UserService
from jmemby.services.tasks import TaskService


class PriorityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'db.sqlite'
        self.store = Store(self.path)
        self.users = UserService(self.store, {1})
        self.users.register(2)
        self.users.register(3)
        self.users.grant(1, 3, True)
        self.manga = Manga('jmcomic', '42', 'Title', (), (Chapter('52', 1, 'Chapter', ()),))
        self.provider = SimpleNamespace(source='jmcomic', get_manga=AsyncMock(return_value=self.manga))
        self.service = TaskService(self.store, self.provider, users=self.users)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def submit(self, source_id, user=2):
        return self.store.submit(replace(self.manga, source_id=source_id), user, user)

    def test_priority_then_fifo(self):
        normal = self.submit('42')
        first = self.submit('43', 3)
        second = self.submit('44', 3)
        for task in (first, second, normal):
            self.assertEqual(self.store.next_task()['id'], task['id'])
            self.store.finish(task['id'], 'COMPLETED')

    async def test_promote_to_tail_without_network_or_ownership_change(self):
        normal = self.submit('42')
        self.store.update(normal['id'], message_id=99)
        first = self.submit('43', 3)
        with self.assertRaisesRegex(ValueError, '移至'):
            await self.service.preview('42', 3, -100)
        self.provider.get_manga.assert_not_awaited()
        promoted = self.store.get(normal['id'])
        self.assertEqual((promoted['user_id'], promoted['chat_id'], promoted['message_id']), (2, 2, 99))
        with self.assertRaisesRegex(ValueError, '拒绝重复'):
            await self.service.preview('42', 3, -100)
        self.assertEqual(self.store.get(normal['id'])['queue_sequence'], promoted['queue_sequence'])
        self.assertEqual(len(self.store.tasks()), 2)
        self.assertEqual(self.store.next_task()['id'], first['id'])
        self.assertEqual(self.store.next_task()['id'], normal['id'])

    def test_confirm_race_commits_promotion_without_binding_other_users_task(self):
        token = self.store.confirmation(self.manga, 3, -100)
        normal = self.submit('42')
        with self.assertRaisesRegex(ValueError, '移至'):
            self.service.confirm(token, 3, -100)
        self.assertEqual(self.store.get(normal['id'])['priority'], 1)
        self.assertIsNone(self.store.connection.execute('SELECT task_id FROM confirmations WHERE token=?', (token,)).fetchone()[0])
        with self.assertRaises(ValueError):
            self.service.get(normal['id'], 3)

    def test_direct_submission_promotion_survives_restart(self):
        normal = self.submit('42')
        with self.assertRaisesRegex(ValueError, '移至'):
            self.submit('42', 3)
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.get(normal['id'])['priority'], 1)
        with self.assertRaises(ValueError):
            self.submit('42', 2)
        self.assertEqual(len(self.store.tasks()), 1)

    def test_running_cancelled_and_completed_not_promoted(self):
        normal = self.submit('42')
        for status in ('DOWNLOADING', 'PACKAGING', 'IMPORT_PENDING', 'CANCEL_REQUESTED', 'COMPLETED'):
            self.store.update(normal['id'], status=status)
            with self.assertRaisesRegex(ValueError, '拒绝重复'):
                self.submit('42', 3)
            self.assertEqual(self.store.get(normal['id'])['priority'], 0)

    def test_retry_delay_and_disk_pause_preserved(self):
        normal = self.submit('42')
        later = time.time() + 3600
        self.store.update(normal['id'], status='RETRY_WAIT', next_at=later, attempts=2)
        with self.assertRaisesRegex(ValueError, '移至'):
            self.submit('42', 3)
        task = self.store.get(normal['id'])
        self.assertEqual((task['status'], task['next_at'], task['attempts']), ('RETRY_WAIT', later, 2))
        other = self.submit('43')
        self.assertEqual(self.store.next_task()['id'], other['id'])
        self.store.update(normal['id'], status='PAUSED_DISK')
        self.assertIsNone(self.store.next_task())

    def test_group_checked_at_confirmation_and_failed_retry(self):
        token = self.store.confirmation(self.manga, 2, 2)
        self.users.grant(1, 2, True)
        task = self.service.confirm(token, 2, 2)
        self.assertEqual(task['priority'], 1)
        self.store.finish(task['id'], 'FAILED')
        self.assertEqual(self.service.retry(task['id'], 2)['priority'], 1)

    def test_v7_migration_assigns_existing_white_jobs_and_is_idempotent(self):
        normal = self.submit('42')
        white = self.submit('43', 3)
        self.store.connection.executescript('DROP INDEX task_created_order; ALTER TABLE tasks DROP COLUMN assigned_node_id; ALTER TABLE tasks DROP COLUMN assigned_node_label; ALTER TABLE tasks DROP COLUMN notify_telegram; DROP TABLE report_deliveries; DROP TABLE manga_reports; DROP TABLE manga_likes; ALTER TABLE manga DROP COLUMN removed_at; DROP TABLE download_nodes; DROP INDEX task_priority_sequence; ALTER TABLE tasks DROP COLUMN priority; PRAGMA user_version=7;')
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.get(normal['id'])['priority'], 0)
        self.assertEqual(self.store.get(white['id'])['priority'], 1)
        self.assertEqual(self.store.connection.execute('PRAGMA user_version').fetchone()[0], 14)
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.next_task()['id'], white['id'])
