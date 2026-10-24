from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from jmemby.db.store import Store
from jmemby.domain.models import Manga, Chapter
from jmemby.services.tasks import TaskService
from jmemby.services.users import UserService
from jmemby.bot.presentation import describe_history
from jmemby.bot.access import chat_allows


class DownloadHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'db.sqlite'
        self.store = Store(self.path)
        self.users = UserService(self.store, {1})
        self.users.register(2, 'reader', 'Reader')
        self.users.register(3)
        self.provider = SimpleNamespace(source='jmcomic', get_manga=AsyncMock())
        self.service = TaskService(self.store, self.provider, ('source.test',), self.users)
        self.manga = Manga('jmcomic', '42', 'Book', (), (Chapter('52', 1, 'Chapter', ()),))

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_failed_first_attempt_and_successful_other_user_are_distinct(self):
        first = self.store.submit(self.manga, 2, -100)
        self.store.finish(first['id'], 'FAILED')
        success = self.store.submit(self.manga, 3, 3)
        self.store.finish(success['id'], 'COMPLETED')
        history = self.service.download_history('https://source.test/album/42', 1)
        self.assertEqual(history['first']['user_id'], 2)
        self.assertEqual(history['completed']['user_id'], 3)
        self.assertEqual(history['total'], 2)
        self.assertIn('首次成功下载者：3', describe_history(history))
        self.provider.get_manga.assert_not_called()

    def test_promotion_does_not_change_downloader_and_restart_preserves_history(self):
        task = self.store.submit(self.manga, 2, 2)
        self.users.grant(1, 3, True)
        with self.assertRaises(ValueError):
            self.store.submit(self.manga, 3, -100)
        self.store.finish(task['id'], 'COMPLETED')
        self.store.close()
        self.store = Store(self.path)
        history = self.store.download_history('jmcomic', '42')
        self.assertEqual(history['completed']['user_id'], 2)
        self.assertEqual(history['total'], 1)

    def test_retry_history_and_pagination(self):
        task = self.store.submit(self.manga, 2, 2)
        for _ in range(6):
            self.store.finish(task['id'], 'FAILED')
            task = self.service.retry(task['id'], 2)
        history = self.service.download_history('42', 1)
        self.assertIsNone(history['completed'])
        self.assertEqual(len(history['tasks']), 5)
        self.assertEqual(len(self.service.download_history('42', 1, 2)['tasks']), 2)
        self.assertTrue(history['tasks'][0]['retry_of'])
        self.assertEqual(self.service.download_history('42', 1, 3)['tasks'], [])
        with self.assertRaises(ValueError):
            self.service.download_history('42', 1, 0)

    def test_admin_without_user_row_and_missing_username_keep_id(self):
        task = self.store.submit(self.manga, 1, 1)
        self.store.finish(task['id'], 'COMPLETED')
        history = self.service.download_history('42', 1)
        self.assertEqual(history['completed']['user_id'], 1)
        self.assertIn('首次成功下载者：1', describe_history(history))

    def test_private_admin_only_and_unknown_manga(self):
        for actor in (2, 3, 99):
            with self.assertRaises(ValueError):
                self.service.download_history('42', actor)
        with self.assertRaisesRegex(ValueError, '暂无'):
            self.service.download_history('42', 1)
        for kind in ('group', 'supergroup', 'channel'):
            self.assertFalse(chat_allows(kind, -100, 1, '/who'))
        self.assertTrue(chat_allows('private', 1, 1, '/who'))
