import asyncio
from dataclasses import replace
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from jmemby.db.store import Store
from jmemby.domain.errors import JobError
from jmemby.domain.models import Chapter, Manga
from jmemby.services.users import UserService
from jmemby.services.tasks import TaskService
from jmemby.web.batch import BatchDownloads


class BatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'db')
        self.users = UserService(self.store, {1, 9})
        self.users.register(2)
        self.manga = Manga('jmcomic', '42', 'Book', (), (Chapter('52', 1, 'Chapter', ()),))

        async def lookup(source_id):
            if source_id == '99':
                raise JobError('NETWORK', '查询失败', True)
            return replace(self.manga, source_id=source_id)

        self.provider = SimpleNamespace(source='jmcomic', get_manga=AsyncMock(side_effect=lookup))
        self.service = TaskService(self.store, self.provider, ('source.test',), self.users)
        self.batches = BatchDownloads(self.service)

    async def asyncTearDown(self):
        await self.batches.close()
        self.store.close()
        self.temp.cleanup()

    async def finish(self, batch):
        await self.batches.jobs[batch['id']]
        return self.batches.snapshot(batch['user_id'], batch['id'])

    async def test_mixed_batch_duplicates_failures_attribution_and_order(self):
        old = self.store.submit(replace(self.manga, source_id='43'), 2, -100)
        self.store.finish(old['id'], 'COMPLETED')
        result = await self.finish(self.batches.start(1, '42\nhttps://source.test/album/42,43，bad;99；44'))
        self.assertEqual([i['status'] for i in result['items']], ['queued', 'skipped', 'skipped', 'invalid', 'failed', 'queued'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['counts']['queued'], 2)
        self.assertEqual([call.args[0] for call in self.provider.get_manga.await_args_list], ['42', '99', '44'])
        for item in (result['items'][0], result['items'][-1]):
            task = self.store.get(item['task_id'])
            self.assertEqual((task['user_id'], task['chat_id'], task['priority']), (1, 1, 0))
            self.assertEqual(task['notify_telegram'], 0)
        self.assertEqual(self.store.next_task()['id'], result['items'][0]['task_id'])
        self.assertEqual(self.store.next_task()['id'], result['items'][-1]['task_id'])

    async def test_limits_and_admin_permissions(self):
        for text in ('', ' '.join(['42']*51), 'x'*16001, None, ['42']):
            with self.assertRaises(ValueError):
                self.batches.start(1, text)
        self.users.grant(1, 2, True)
        for actor in (2, 77):
            with self.assertRaises(ValueError):
                self.batches.start(actor, '42')
        self.provider.get_manga.assert_not_awaited()
        result = await self.finish(self.batches.start(1, ' '.join(str(100+i) for i in range(50))))
        self.assertEqual(result['counts']['queued'], 50)

    async def test_one_active_batch_per_admin_and_serial_provider_calls(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow(source_id):
            entered.set()
            await release.wait()
            return replace(self.manga, source_id=source_id)

        self.provider.get_manga.side_effect = slow
        first = self.batches.start(1, '42')
        await entered.wait()
        with self.assertRaises(ValueError):
            self.batches.start(1, '43')
        second = self.batches.start(9, '44')
        await asyncio.sleep(0)
        self.assertEqual(self.provider.get_manga.await_count, 1)
        with self.assertRaises(ValueError):
            self.batches.snapshot(9, first['id'])
        self.assertEqual(self.batches.latest(1)['id'], first['id'])
        release.set()
        await asyncio.gather(*list(self.batches.jobs.values()))
        self.assertEqual(self.batches.snapshot(9, second['id'])['counts']['queued'], 1)

    async def test_admin_rechecked_after_network_and_shutdown_leaves_queued_tasks(self):
        async def revoke(source_id):
            self.users.admin_ids = frozenset({9})
            return self.manga
        self.provider.get_manga.side_effect = revoke
        batch = self.batches.start(1, '42')
        await self.batches.jobs[batch['id']]
        self.assertEqual(self.store.tasks(), [])
        with self.assertRaises(ValueError):
            self.batches.snapshot(1, batch['id'])

    async def test_close_interrupts_lookup_but_preserves_already_queued_task(self):
        blocked = asyncio.Event()
        async def lookup(source_id):
            if source_id == '43':
                blocked.set()
                await asyncio.Event().wait()
            return self.manga
        self.provider.get_manga.side_effect = lookup
        batch = self.batches.start(1, '42 43 44')
        await blocked.wait()
        await self.batches.close()
        result = self.batches.snapshot(1, batch['id'])
        self.assertEqual(result['status'], 'interrupted')
        self.assertEqual(result['counts']['queued'], 1)
        self.assertEqual(len(self.store.tasks()), 1)

    async def test_promotion_reuses_original_task_and_does_not_transfer_ownership(self):
        # Fixture representing an administrator with an existing white user row.
        self.store.connection.execute("INSERT INTO users(user_id,user_group,registered_at) VALUES (1,'white',0)")
        old = self.store.submit(self.manga, 2, -100)
        result = await self.finish(self.batches.start(1, '42 43'))
        self.assertEqual(result['items'][0]['status'], 'skipped')
        self.assertIn('移至', result['items'][0]['message'])
        original = self.store.get(old['id'])
        self.assertEqual((original['priority'], original['user_id'], original['chat_id']), (1, 2, -100))
        self.assertEqual(original['notify_telegram'], 1)
        self.assertEqual(self.store.get(result['items'][1]['task_id'])['priority'], 1)

    async def test_batch_silent_for_all_terminal_states_and_after_restart(self):
        result=await self.finish(self.batches.start(1,'42 43 44'))
        task_ids=[i['task_id'] for i in result['items']]
        # Even an accidentally set message id cannot enroll a silent task in progress updates.
        for task_id in task_ids:self.store.update(task_id,message_id=99)
        self.assertEqual(self.store.tasks(active=True,telegram_only=True),[])
        path=Path(self.temp.name)/'db'
        reopened=Store(path)
        try:
            self.assertTrue(all(reopened.get(t)['notify_telegram']==0 for t in task_ids))
        finally:reopened.close()
        for task_id,status in zip(task_ids,('COMPLETED','FAILED','CANCELLED')):
            self.store.finish(task_id,status)
        self.assertEqual(self.store.notifications(),[])
        self.assertEqual(self.store.connection.execute('SELECT COUNT(*) FROM notifications').fetchone()[0],0)
        retry=self.service.retry(task_ids[1],1)
        self.assertEqual(retry['notify_telegram'],0)

    async def test_silent_batches_do_not_hide_or_mute_manual_tasks(self):
        manual=self.store.submit(self.manga,2,-100)
        await self.finish(self.batches.start(1,' '.join(str(n) for n in range(100,125))))
        self.assertEqual([t['id'] for t in self.store.tasks(active=True,telegram_only=True)],[manual['id']])
        self.store.finish(manual['id'],'COMPLETED')
        self.assertEqual([n['task_id'] for n in self.store.notifications()],[manual['id']])

    async def test_finished_results_expire_without_deleting_download_tasks(self):
        batch = await self.finish(self.batches.start(1, '42'))
        touched = self.batches.batches[batch['id']]['touched']
        with patch('jmemby.web.batch.time.monotonic', return_value=touched+3601):
            self.assertIsNone(self.batches.latest(1))
        self.assertEqual(len(self.store.tasks()), 1)
