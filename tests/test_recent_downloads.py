from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from jmemby.db.store import Store
from jmemby.domain.models import Chapter, Manga
from jmemby.services.tasks import TaskService
from jmemby.services.users import UserService


class RecentDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'db')
        self.users = UserService(self.store, {1})
        self.users.register(2, 'ReaderName', '读者甲')
        self.service = TaskService(self.store, None, users=self.users)
        self.manga = Manga('jmcomic', '42', 'Title', (), (Chapter('52', 1, 'Chapter', ()),))

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_latest_100_and_search_finds_older_history(self):
        tasks = []
        for i in range(105):
            item = replace(self.manga, source_id=str(1000 + i), title='Older needle' if i == 0 else 'Book')
            task = self.store.submit(item, 2, 2)
            # Identical timestamps exercise deterministic insertion-order tie breaking.
            self.store.connection.execute('UPDATE tasks SET created_at=100 WHERE id=?', (task['id'],))
            tasks.append(task)
        rows = self.service.recent_downloads(1)
        self.assertEqual([t['id'] for t in rows], [t['id'] for t in reversed(tasks[5:])])
        self.assertEqual(self.service.recent_downloads(1, 'needle')[0]['id'], tasks[0]['id'])
        self.assertEqual(len(self.service.recent_downloads(1, 'Book')), 100)

    def test_search_fields_case_and_literal_special_characters(self):
        task = self.store.submit(replace(self.manga, title='冒险 100%_故事'), 2, 2)
        for query in ('冒险', '42', task['id'], '2', '@readername', 'ReaderName', '读者甲', '%_', '  冒险  '):
            with self.subTest(query=query):
                self.assertEqual(self.service.recent_downloads(1, query)[0]['id'], task['id'])
        for query in ("' OR 1=1 --", '%missing', 'unknown'):
            self.assertEqual(self.service.recent_downloads(1, query), [])

    def test_all_statuses_and_missing_user_record(self):
        for i, status in enumerate(('QUEUED', 'FAILED', 'CANCELLED', 'COMPLETED', 'DOWNLOADING')):
            task = self.store.submit(replace(self.manga, source_id=str(i+100)), 1, 1)
            self.store.update(task['id'], status=status)
        rows = self.service.recent_downloads(1)
        self.assertEqual(len(rows), 5)
        self.assertTrue(all(t['user_id'] == 1 and t['username'] is None for t in rows))
        self.assertEqual({t['status'] for t in rows}, {'QUEUED', 'FAILED', 'CANCELLED', 'COMPLETED', 'DOWNLOADING'})

    def test_queue_promotion_does_not_reorder_submission_history(self):
        old = self.store.submit(self.manga, 2, 2)
        new = self.store.submit(replace(self.manga, source_id='43'), 2, 2)
        self.store.connection.execute('UPDATE tasks SET created_at=100')
        self.users.register(3)
        self.users.grant(1, 3, True)
        with self.assertRaises(ValueError):
            self.store.submit(self.manga, 3, 3)
        rows = self.service.recent_downloads(1)
        self.assertEqual([t['id'] for t in rows], [new['id'], old['id']])
        self.assertEqual((rows[1]['user_id'], rows[1]['priority']), (2, 1))

    def test_admin_only_empty_and_search_length(self):
        self.assertEqual(self.service.recent_downloads(1), [])
        self.users.grant(1, 2, True)
        for user in (2, 999):
            with self.assertRaises(ValueError):
                self.service.recent_downloads(user)
        with self.assertRaises(ValueError):
            self.service.recent_downloads(1, 'x' * 201)
        with self.assertRaises(ValueError):
            TaskService(self.store, None).recent_downloads(1)
