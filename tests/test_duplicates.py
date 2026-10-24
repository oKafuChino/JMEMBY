from dataclasses import replace
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from jmemby.db.store import Store
from jmemby.domain.models import Chapter, Manga
from jmemby.services.tasks import TaskService


class DuplicateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'app.db'
        self.store = Store(self.path)
        self.manga = Manga('jmcomic', '42', 'Title', (), (Chapter('52', 1, 'Chapter', ()),))
        self.provider = SimpleNamespace(source='jmcomic', get_manga=AsyncMock(return_value=self.manga))
        self.service = TaskService(self.store, self.provider, ('source.test',))

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    async def test_completed_id_and_link_rejected_before_network_across_users_and_restart(self):
        task = self.store.submit(self.manga, 1, 1)
        self.store.finish(task['id'], 'COMPLETED')
        self.store.close()
        self.store = Store(self.path)
        self.service.store = self.store
        for reference in ('42', 'https://source.test/album/42'):
            for user, chat in ((1, 1), (2, -100)):
                with self.assertRaisesRegex(ValueError, '已下载完成'):
                    await self.service.preview(reference, user, chat)
        self.provider.get_manga.assert_not_awaited()
        self.assertEqual(self.store.connection.execute('SELECT COUNT(*) FROM confirmations').fetchone()[0], 0)

    async def test_all_active_states_reject_duplicates(self):
        task = self.store.submit(self.manga, 1, 1)
        for state in ('QUEUED', 'DOWNLOADING', 'PACKAGING', 'IMPORT_PENDING', 'RETRY_WAIT', 'PAUSED_DISK', 'CANCEL_REQUESTED'):
            self.store.update(task['id'], status=state)
            with self.assertRaisesRegex(ValueError, '进行中'):
                await self.service.preview('42', 2, -100)
            with self.assertRaisesRegex(ValueError, '进行中'):
                self.store.submit(self.manga, 1, 1)
        self.provider.get_manga.assert_not_awaited()

    async def test_task_created_during_metadata_lookup_is_rejected(self):
        async def lookup(source_id):
            self.store.submit(self.manga, 2, 2)
            return self.manga
        self.provider.get_manga.side_effect = lookup
        with self.assertRaisesRegex(ValueError, '拒绝重复下载'):
            await self.service.preview('42', 1, -100)
        self.assertEqual(self.store.connection.execute('SELECT COUNT(*) FROM confirmations').fetchone()[0], 0)

    def test_two_previews_then_completion_cannot_enqueue_again(self):
        first = self.store.confirmation(self.manga, 1, 1)
        second = self.store.confirmation(self.manga, 2, -100)
        task = self.store.confirm(first, 1, 1)
        with self.assertRaisesRegex(ValueError, '进行中'):
            self.store.confirm(second, 2, -100)
        self.store.finish(task['id'], 'COMPLETED')
        with self.assertRaisesRegex(ValueError, '已下载完成'):
            self.store.confirm(second, 2, -100)
        self.assertEqual(self.store.confirm(first, 1, 1)['id'], task['id'])
        self.assertEqual(len(self.store.tasks()), 1)

    def test_failed_and_cancelled_can_be_resubmitted(self):
        for state in ('FAILED', 'CANCELLED'):
            task = self.store.submit(self.manga, 1, 1)
            self.store.finish(task['id'], state)
        self.assertEqual(self.store.submit(self.manga, 2, -100)['status'], 'QUEUED')

    def test_title_changes_do_not_bypass_id_but_same_title_different_id_allowed(self):
        task = self.store.submit(self.manga, 1, 1)
        self.store.finish(task['id'], 'COMPLETED')
        with self.assertRaisesRegex(ValueError, '已下载完成'):
            self.store.submit(replace(self.manga, title='Renamed'), 2, -100)
        self.store.submit(replace(self.manga, source_id='43'), 2, -100)
        self.store.submit(replace(self.manga, source='other'), 2, -100)

    def test_old_failed_task_cannot_retry_after_later_success(self):
        failed = self.store.submit(self.manga, 1, 1)
        self.store.finish(failed['id'], 'FAILED')
        done = self.service.retry(failed['id'], 1)
        self.store.finish(done['id'], 'COMPLETED')
        with self.assertRaisesRegex(ValueError, '已下载完成'):
            self.service.retry(failed['id'], 1)
