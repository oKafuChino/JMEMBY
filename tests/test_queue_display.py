from pathlib import Path
import tempfile
import unittest

from jmemby.db.store import Store
from jmemby.distributed.config import MasterSettings
from jmemby.distributed.coordinator import Coordinator
from jmemby.domain.models import Chapter, Manga
from jmemby.services.tasks import TaskService
from jmemby.services.users import UserService


class QueueDisplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'db'
        self.store = Store(self.path)
        self.users = UserService(self.store, {1})
        self.users.register(2)
        self.users.grant(1, 3, True)
        self.users.register(3)
        self.service = TaskService(self.store, None, users=self.users)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def submit(self, number, user=2):
        manga = Manga('jmcomic', str(number), 'Book', (), (Chapter(str(number), 1, 'Chapter', ()),))
        return self.store.submit(manga, user, user), manga

    def test_time_order_across_statuses_and_priorities_without_changing_claim_order(self):
        tasks = [self.submit(i, 3 if i % 2 else 2)[0] for i in range(25)]
        for i, task in enumerate(tasks):
            self.store.connection.execute('UPDATE tasks SET created_at=? WHERE id=?', (i // 2, task['id']))
        self.store.finish(tasks[-1]['id'], 'COMPLETED')
        self.store.update(tasks[-2]['id'], status='FAILED')
        expected = sorted(enumerate(tasks), key=lambda item: (item[0] // 2, item[1]['id']), reverse=True)
        expected_ids = [task['id'] for _, task in expected]
        first = self.service.queue_page(1)
        second = self.service.queue_page(1, 2)
        self.assertEqual((len(first), len(second)), (20, 5))
        self.assertEqual([t['id'] for t in first + second], expected_ids)
        self.assertEqual(self.service.queue_page(1, 3), [])
        for queue, priority in (('normal', 0), ('white', 1)):
            rows = self.service.queue_page(1, queue=queue)
            self.assertEqual([t['id'] for t in rows], [t['id'] for _, t in expected if t['priority'] == priority])
        self.assertEqual(self.store.next_task()['id'], tasks[1]['id'])

    def test_saved_node_survives_restart_and_live_assignment_overrides_it(self):
        task, manga = self.submit(42)
        self.assertIsNone(self.service.queue_page(1)[0]['node'])
        self.store.update(task['id'], assigned_node_id='vps-1', assigned_node_label='First node')
        coordinator = self.service.download_coordinator = Coordinator(MasterSettings(True, {'vps-2': 'token'}))
        coordinator.assign('vps-2', task, manga)
        row = self.service.queue_page(1)[0]
        self.assertEqual(row['node'], {'id': 'vps-2', 'label': 'vps-2'})
        self.assertTrue(row['node_current'])
        self.store.update(task['id'], assigned_node_id='vps-2', assigned_node_label='Second node')
        coordinator.unassign('vps-2')
        self.store.finish(task['id'], 'COMPLETED')
        self.store.close()
        self.store = Store(self.path)
        self.service = TaskService(self.store, None, users=UserService(self.store, {1}))
        row = self.service.queue_page(1)[0]
        self.assertEqual(row['node'], {'id': 'vps-2', 'label': 'Second node'})
        self.assertFalse(row['node_current'])
        self.assertNotIn('assigned_node_id', row)
        self.store.update(task['id'], assigned_node_label=None)
        self.assertEqual(self.service.queue_page(1)[0]['node']['label'], 'vps-2')

    def test_admin_only_and_invalid_filters(self):
        for actor in (2, 3, 99):
            with self.assertRaises(ValueError):
                self.service.queue_page(actor)
        with self.assertRaises(ValueError):
            TaskService(self.store, None).queue_page(1)
        for page in (0, -1, True, '1', 1.5, 1000001):
            with self.assertRaises(ValueError):
                self.service.queue_page(1, page)
        with self.assertRaises(ValueError):
            self.service.queue_page(1, queue="' OR 1=1 --")

    def test_schema_13_upgrade_preserves_tasks_without_inventing_node_history(self):
        task, _ = self.submit(42)
        self.store.finish(task['id'], 'COMPLETED')
        before = self.store.get(task['id'])
        before.pop('assigned_node_id')
        before.pop('assigned_node_label')
        self.store.connection.executescript('''
            DROP INDEX task_created_order;
            ALTER TABLE tasks DROP COLUMN assigned_node_id;
            ALTER TABLE tasks DROP COLUMN assigned_node_label;
            PRAGMA user_version=13;
        ''')
        for _ in range(2):
            self.store.close()
            self.store = Store(self.path)
            self.assertEqual(self.store.connection.execute('PRAGMA user_version').fetchone()[0], 14)
            self.assertEqual(self.store.get(task['id']), {**before, 'assigned_node_id': None, 'assigned_node_label': None})


if __name__ == '__main__':
    unittest.main()
