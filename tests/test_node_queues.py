from pathlib import Path
import tempfile
import time
import unittest

from jmemby.db.store import Store
from jmemby.distributed.nodes import NodeRegistry
from jmemby.domain.models import Chapter, Manga
from jmemby.services.users import UserService


class NodeQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/'db'
        self.store=Store(self.path)
        self.registry=NodeRegistry(self.store,{'seed':'a'*43})
        users=UserService(self.store,{1})
        users.register(2);users.grant(1,3,True);users.register(3)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def submit(self, number, white=False):
        manga=Manga('jmcomic',str(number),'Book',(),(Chapter(str(number),1,'Chapter',()),))
        return self.store.submit(manga,3 if white else 2,-100)

    def test_normal_only_skips_white_and_white_only_skips_normal(self):
        normal=self.submit(1);white=self.submit(2,True)
        self.assertEqual(self.store.next_task(queue_scope='normal')['id'],normal['id'])
        self.assertIsNone(self.store.next_task(queue_scope='normal'))
        self.assertEqual(self.store.get(white['id'])['status'],'QUEUED')
        self.assertEqual(self.store.next_task(queue_scope='white')['id'],white['id'])
        self.assertIsNone(self.store.next_task(queue_scope='all'))

    def test_both_queues_keep_white_priority_and_fifo(self):
        n1=self.submit(1);w1=self.submit(2,True);n2=self.submit(3);w2=self.submit(4,True)
        self.assertEqual([self.store.next_task(queue_scope='all')['id'] for _ in range(4)],
                         [w1['id'],w2['id'],n1['id'],n2['id']])

    def test_filter_preserves_retry_delays_cancellations_and_active_exclusion(self):
        retry=self.submit(1,True);active=self.submit(2,True);ready=self.submit(3,True)
        normal=self.submit(4)
        self.store.update(retry['id'],status='RETRY_WAIT',next_at=time.time()+3600)
        self.store.update(active['id'],status='CANCEL_REQUESTED',cancel_requested=1)
        self.assertEqual(self.store.next_task(exclude={active['id']},queue_scope='white')['id'],ready['id'])
        self.assertIsNone(self.store.next_task(exclude={active['id']},queue_scope='white'))
        self.assertEqual(self.store.get(retry['id'])['attempts'],0)
        self.assertEqual(self.store.get(normal['id'])['status'],'QUEUED')
        self.store.update(retry['id'],next_at=0)
        self.assertEqual(self.store.next_task(exclude={active['id']},queue_scope='white')['id'],retry['id'])

    def test_promoted_task_routes_by_queue_without_changing_original_owner(self):
        task=self.submit(1)
        with self.assertRaises(ValueError):self.submit(1,True)
        self.assertIsNone(self.store.next_task(queue_scope='normal'))
        claimed=self.store.next_task(queue_scope='white')
        self.assertEqual((claimed['id'],claimed['user_id'],claimed['chat_id']),(task['id'],2,-100))

    def test_create_edit_persistence_defaults_and_no_revision_change(self):
        self.assertEqual(self.registry.public('seed')['queue_scope'],'all')
        self.registry.create('new','New',1,'normal')
        revision=self.registry.get('new')['revision']
        self.registry.update('new','Updated',2,'white')
        self.registry.update('new','Label only',3)
        self.assertEqual(self.registry.public('new')['queue_scope'],'white')
        self.assertEqual(self.registry.get('new')['revision'],revision)
        self.store.close();self.store=Store(self.path);self.registry=NodeRegistry(self.store)
        self.assertEqual(self.registry.public('new')['queue_scope'],'white')
        self.registry.delete('new');self.registry.create('new','Recreated',1)
        self.assertEqual(self.registry.public('new')['queue_scope'],'all')

    def test_bad_scope_rejected_without_modifying_node_or_claiming_task(self):
        task=self.submit(1)
        for scope in ('both','',None,True,1,[],{},"normal' OR 1=1 --"):
            with self.subTest(scope=scope):
                with self.assertRaises(ValueError):self.registry.create('new','New',1,scope)
                with self.assertRaises(ValueError):self.registry.update('seed','Changed',2,scope)
                with self.assertRaises(ValueError):self.store.next_task(queue_scope=scope)
        self.assertEqual(self.registry.get('seed')['label'],'seed')
        self.assertEqual(self.store.get(task['id'])['status'],'QUEUED')

    def test_schema_nine_upgrade_preserves_nodes_credentials_and_tasks(self):
        task=self.submit(1,True)
        self.registry.set_enabled('seed',False)
        before=self.registry.get('seed')
        self.store.connection.executescript('DROP TABLE channel_deliveries; DROP INDEX task_created_order; ALTER TABLE tasks DROP COLUMN assigned_node_id; ALTER TABLE tasks DROP COLUMN assigned_node_label; ALTER TABLE tasks DROP COLUMN notify_telegram; DROP TABLE report_deliveries; DROP TABLE manga_reports; DROP TABLE manga_likes; ALTER TABLE manga DROP COLUMN removed_at; ALTER TABLE download_nodes DROP COLUMN queue_scope; PRAGMA user_version=9;')
        self.store.close();self.store=Store(self.path);self.registry=NodeRegistry(self.store)
        after=self.registry.get('seed')
        self.assertEqual(after['queue_scope'],'all')
        for key in ('label','enabled','revision','token_hash','created_at'):
            self.assertEqual(after[key],before[key])
        self.assertEqual(self.store.get(task['id'])['priority'],1)
        self.assertEqual(self.store.connection.execute('PRAGMA user_version').fetchone()[0],15)
        self.store.close();self.store=Store(self.path)
        self.assertEqual(NodeRegistry(self.store).public('seed')['queue_scope'],'all')
