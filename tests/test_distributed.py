import asyncio
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from jmemby.config import Settings
from jmemby.db.store import Store
from jmemby.distributed.agent import Agent
from jmemby.distributed.config import AgentSettings, MasterSettings
from jmemby.distributed.coordinator import Coordinator, LeaseConflict
from jmemby.distributed.workers import WorkerPool
from jmemby.domain.errors import JobError
from jmemby.domain.models import Chapter, Manga, Page
from jmemby.providers.command import CommandProvider
from jmemby.services.users import UserService
from jmemby.storage.cbz import validate_image


TOKENS = {'vps-1': 'a' * 43, 'vps-2': 'b' * 43}


def image_bytes():
    stream = io.BytesIO()
    Image.new('RGB', (8, 8), 'white').save(stream, 'PNG')
    return stream.getvalue()


def manga(number):
    return Manga('jmcomic', str(number), f'Book {number}', (),
                 (Chapter(str(number * 10), 1, 'First', ()),
                  Chapter(str(number * 10 + 1), 2, 'Second', ())))


class ConfigTests(unittest.TestCase):
    def test_remote_mode_is_mandatory_even_for_legacy_false(self):
        for value in ('', 'false', '0', 'true'):
            with patch.dict(os.environ, {'DISTRIBUTED_ENABLED':value}, clear=True):
                self.assertTrue(MasterSettings.from_env().enabled)
                self.assertEqual(MasterSettings.from_env().tokens,{})

    def test_master_tokens_and_limits(self):
        environment = {'DISTRIBUTED_ENABLED': 'true', 'DOWNLOAD_AGENT_TOKENS': json.dumps(TOKENS)}
        with patch.dict(os.environ, environment, clear=True):
            config = MasterSettings.from_env()
            self.assertEqual(config.tokens, TOKENS)
            self.assertNotIn('a' * 43, repr(config))
            for invalid in ('[]', '{"a":"short"}', json.dumps({'x': 'a' * 43, 'y': 'a' * 43})):
                with patch.dict(os.environ, {'DOWNLOAD_AGENT_TOKENS': invalid}):
                    with self.assertRaises(ValueError):
                        MasterSettings.from_env()

            with patch.dict(os.environ, {'DISTRIBUTED_LEASE_SECONDS': '0'}):
                with self.assertRaises(ValueError):
                    MasterSettings.from_env()
            with patch.dict(os.environ, {'PROVIDER_COMMAND': '["custom"]'}):
                with self.assertRaises(ValueError):
                    MasterSettings.from_env()

    def test_no_local_fallback_when_legacy_switch_disabled(self):
        with patch.dict(os.environ, {'DISTRIBUTED_ENABLED':'false', 'PROVIDER_COMMAND':'["custom"]'}, clear=True):
            with self.assertRaises(ValueError):
                MasterSettings.from_env()

    def test_agent_needs_no_bot_or_komga_credentials(self):
        with patch.dict(os.environ, {'AGENT_MASTER_URL': 'https://master.example',
                                    'AGENT_ID': 'vps-1', 'AGENT_TOKEN': TOKENS['vps-1']}, clear=True):
            self.assertEqual(AgentSettings.from_env().concurrency, 1)
            for origin in ('http://public.example', 'https://user:pass@example.com', 'https://example.com/path'):
                with patch.dict(os.environ, {'AGENT_MASTER_URL': origin}):
                    with self.assertRaises(ValueError):
                        AgentSettings.from_env()
            with patch.dict(os.environ, {'AGENT_CONCURRENCY': '0'}):
                with self.assertRaises(ValueError):
                    AgentSettings.from_env()

    def test_secrets_not_in_provider_subprocess(self):
        env = {'AGENT_TOKEN': 'secret', 'DOWNLOAD_AGENT_TOKENS': 'secrets',
               'TELEGRAM_BOT_TOKEN': 'bot', 'KOMGA_PASSWORD': 'password', 'JM_API_DOMAINS': 'example.test'}
        with patch.dict(os.environ, env, clear=True):
            child = CommandProvider('["python"]').child_environment()
            self.assertEqual(child, {'JM_API_DOMAINS': 'example.test'})


class CoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.now = 100
        self.coordinator = Coordinator(MasterSettings(True, TOKENS), lambda: self.now)
        self.coordinator.touch('vps-1')
        self.coordinator.assign('vps-1', {'id': 'task-1'}, manga(1))
        self.operations = []

    async def asyncTearDown(self):
        for task in self.operations:
            task.cancel()
        await asyncio.gather(*self.operations, return_exceptions=True)
        self.temp.cleanup()

    async def start(self, count=2, budget=100000):
        task = asyncio.create_task(self.coordinator.download_batch(
            'vps-1', [Page(i, f'jm:10:{i-1}') for i in range(1, count + 1)],
            self.root, 100000, 10000, budget))
        self.operations.append(task)
        await asyncio.sleep(0)
        return task, self.coordinator.claim('vps-1')

    def upload(self, job, number):
        temp = self.root / 'upload.part'
        temp.write_bytes(image_bytes())
        extension = validate_image(temp)
        self.coordinator.accept(job['id'], 'vps-1', job['lease'], number, temp, extension)
        temp.unlink(missing_ok=True)

    async def test_manga_pinned_to_agent_and_idempotent_page(self):
        task, job = await self.start()
        self.assertIsNone(self.coordinator.claim('vps-2'))
        with self.assertRaises(RuntimeError):
            self.coordinator.assign('vps-1', {'id': 'other-task'}, manga(2))
        with self.assertRaises(LeaseConflict):
            self.coordinator.heartbeat(job['id'], 'vps-2', job['lease'])
        self.upload(job, 1)
        before = self.coordinator.jobs[job['id']].received
        self.upload(job, 1)
        self.assertEqual(self.coordinator.jobs[job['id']].received, before)
        with self.assertRaises(LeaseConflict):
            self.coordinator.finish(job['id'], 'vps-1', job['lease'])
        self.upload(job, 2)
        self.coordinator.finish(job['id'], 'vps-1', job['lease'])
        await asyncio.wait_for(task, 1)
        self.assertIn('vps-1', self.coordinator.books)  # Same VPS retains the next chapter.
        _, following = await self.start(1)
        self.assertEqual(following['book']['task_id'], 'task-1')
        self.assertIsNone(self.coordinator.claim('vps-2'))

    async def test_lease_expiry_rejects_late_result(self):
        task, job = await self.start()
        self.now += 91
        with self.assertRaises(LeaseConflict):
            self.upload(job, 1)
        with self.assertRaises(JobError) as error:
            await asyncio.wait_for(task, 1)
        self.assertEqual(error.exception.code, 'AGENT_OFFLINE')
        self.assertFalse((self.root / '000001.png').exists())

    async def test_heartbeat_and_hard_deadline(self):
        task, job = await self.start()
        self.now += 70
        self.coordinator.heartbeat(job['id'], 'vps-1', job['lease'])
        self.now += 40
        self.coordinator.owned(job['id'], 'vps-1', job['lease'])
        self.now = 2001
        with self.assertRaises(JobError) as error:
            await asyncio.wait_for(task, 1)
        self.assertEqual(error.exception.code, 'AGENT_TIMEOUT')

    async def test_cancellation_invalidates_lease(self):
        task, job = await self.start()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        with self.assertRaises(LeaseConflict):
            self.upload(job, 1)
        self.assertFalse(self.coordinator.jobs)

    async def test_budget_is_global_and_duplicate_not_counted(self):
        task, job = await self.start(budget=len(image_bytes()))
        self.upload(job, 1)
        with self.assertRaises(JobError):
            self.upload(job, 2)
        with self.assertRaises(JobError):
            await asyncio.wait_for(task, 1)
        self.assertFalse((self.root / '000002.png').exists())

    async def test_failure_and_offline_before_claim(self):
        task, job = await self.start()
        self.coordinator.fail(job['id'], 'vps-1', job['lease'])
        with self.assertRaises(JobError):
            await asyncio.wait_for(task, 1)
        task = asyncio.create_task(self.coordinator.download_batch(
            'vps-1', [Page(1, 'jm:10:0')], self.root, 100000, 10000, 100000))
        self.operations.append(task)
        self.now += 100
        with self.assertRaises(JobError):
            await asyncio.wait_for(task, 1)

    def test_duplicate_agent_process_is_rejected(self):
        self.coordinator.connect('vps-1', 'a' * 32)
        with self.assertRaises(LeaseConflict):
            self.coordinator.connect('vps-1', 'b' * 32)
        self.now += 100
        self.coordinator.connect('vps-1', 'b' * 32)


class FakeProvider:
    source = 'jmcomic'

    def __init__(self):
        self.calls = []
        self.active = 0
        self.peak = 0

    async def get_pages(self, chapter):
        return tuple(Page(i, f'jm:{chapter}:{i-1}') for i in range(1, 18))

    async def download_batch(self, pages, stage, max_bytes, max_pixels, budget):
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.calls.append(pages[0].url.split(':')[1])
        try:
            await asyncio.sleep(0.01)
            for page in pages:
                (stage / f'{page.number:06d}.png').write_bytes(image_bytes())
        finally:
            self.active -= 1


class LocalTransportAgent(Agent):
    """Exercise real agent execution through an in-memory, validated transport."""
    def __init__(self, settings, coordinator, provider):
        super().__init__(settings, provider)
        self.coordinator = coordinator

    async def request(self, session, method, path, **kwargs):
        parts = path.split('/')
        if path == 'claim':
            return {'job': self.coordinator.claim(self.settings.agent_id)}
        identity = (parts[1], self.settings.agent_id, kwargs['headers']['X-Lease'])
        if parts[2] == 'pages':
            job = self.coordinator.owned(*identity)
            content = kwargs['data'].read()
            assert hashlib.sha256(content).hexdigest() == kwargs['headers']['X-Content-SHA256']
            tmp = job.stage / f'test-{parts[3]}.part'
            tmp.write_bytes(content)
            self.coordinator.accept(*identity, int(parts[3]), tmp, validate_image(tmp))
            tmp.unlink(missing_ok=True)
        else:
            getattr(self.coordinator, {'complete': 'finish'}.get(parts[2], parts[2]))(*identity)
        return {'ok': True}


class PoolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'db')
        self.settings = Settings('token', frozenset({1}), str(self.root / 'db'), str(self.root / 'storage'),
                                 'http://komga', '', 'library', '/data', max_task_bytes=1000000)
        self.coordinator = Coordinator(MasterSettings(True, TOKENS))
        class Komga:
            async def import_paths(self, paths):
                return {p: str(i) for i, p in enumerate(paths)}
        self.pool = WorkerPool(self.store, FakeProvider(), Komga(), self.settings, self.coordinator)

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def pump(self, name, operation, remote_provider):
        settings = AgentSettings('https://master.example', name, TOKENS[name],
                                 concurrency=2, work_dir=str(self.root / name), min_free_space_mb=0, max_page_bytes=10000)
        agent = LocalTransportAgent(settings, self.coordinator, remote_provider)
        seen = []
        while not operation.done():
            job = self.coordinator.claim(name)
            if job:
                seen.append(job['book']['source_id'])
                await agent.process(None, job)
            else:
                await asyncio.sleep(0.01)
        return seen

    async def test_two_vps_two_whole_books_parallel_priority_preserved(self):
        users = UserService(self.store, {1})
        users.register(2)
        users.grant(1, 3, True)
        users.register(3)
        normal = self.store.submit(manga(1), 2, 2)
        white = self.store.submit(manga(2), 3, 3)
        waiting = self.store.submit(manga(3), 2, 2)
        for name in TOKENS:
            self.coordinator.touch(name)
        first = asyncio.create_task(self.pool.workers[0].run_once())
        second = asyncio.create_task(self.pool.workers[1].run_once())
        providers = [FakeProvider(), FakeProvider()]
        try:
            results = await asyncio.wait_for(asyncio.gather(
                self.pump('vps-1', first, providers[0]), self.pump('vps-2', second, providers[1])), 10)
            await asyncio.gather(first, second)
            self.assertEqual(results, [['2', '2'], ['1', '1']])
            self.assertTrue(all(1 < p.peak <= 2 for p in providers))
            self.assertEqual(self.store.get(white['id'])['status'], 'COMPLETED')
            self.assertEqual(self.store.get(normal['id'])['status'], 'COMPLETED')
            self.assertEqual(self.store.get(waiting['id'])['status'], 'QUEUED')
            self.assertEqual(len(list((self.root / 'storage/library').rglob('*.cbz'))), 4)
            self.assertFalse(self.coordinator.books)
            self.assertFalse(self.pool.workers[0].active_tasks)
        finally:
            for task in (first, second): task.cancel()
            await asyncio.gather(first, second, return_exceptions=True)

    async def test_offline_nodes_leave_queue_untouched_recovery_once(self):
        task = self.store.submit(manga(1), 1, 1)
        with patch.object(self.store, 'recover', wraps=self.store.recover) as recover:
            runner = asyncio.create_task(self.pool.run())
            try:
                await asyncio.sleep(0.05)
                self.assertEqual(self.store.get(task['id'])['status'], 'QUEUED')
                recover.assert_called_once()
            finally:
                runner.cancel()
                await asyncio.gather(runner, return_exceptions=True)

    async def test_cancellation_cannot_be_claimed_by_second_worker(self):
        first = self.store.submit(manga(1), 1, 1)
        second = self.store.submit(manga(2), 1, 1)
        self.store.next_task()
        self.store.request_cancel(first['id'], 1)
        claimed = self.store.next_task(exclude={first['id']})
        self.assertEqual(claimed['id'], second['id'])

    async def test_empty_pool_add_delete_and_recreate_without_restart(self):
        from jmemby.distributed.nodes import NodeRegistry
        self.coordinator.registry = NodeRegistry(self.store)
        pool = WorkerPool(self.store, FakeProvider(), self.pool.komga, self.settings, self.coordinator)
        self.assertEqual(pool.workers, [])
        self.assertGreater(pool.heartbeat, 0)
        runner = asyncio.create_task(pool.run())
        async def wait_for_agents(names):
            async with asyncio.timeout(3):
                while sorted(w.agent for w in pool.workers) != names:
                    await asyncio.sleep(0.02)
        try:
            await asyncio.sleep(0)
            self.coordinator.registry.create('new', 'New', 2)
            await wait_for_agents(['new'])
            first = pool.workers[0]
            self.coordinator.registry.delete('new')
            await wait_for_agents([])
            self.coordinator.registry.create('new', 'New again', 1)
            await wait_for_agents(['new'])
            self.assertIsNot(first, pool.workers[0])
            self.assertIs(pool.workers[0].active_tasks, pool.active_tasks)
        finally:
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)

    async def test_disable_running_node_requeues_with_original_owner_and_priority(self):
        from jmemby.distributed.nodes import NodeRegistry
        self.coordinator.registry = NodeRegistry(self.store, TOKENS)
        users = UserService(self.store, {1})
        users.grant(1, 42, True)
        users.register(42)
        task = self.store.submit(manga(1), 42, -100)
        self.coordinator.touch('vps-1')
        work = asyncio.create_task(self.pool.workers[0].run_once())
        try:
            async with asyncio.timeout(3):
                while not self.coordinator.jobs:
                    await asyncio.sleep(0.01)
            self.coordinator.registry.set_enabled('vps-1', False)
            self.coordinator.invalidate('vps-1')
            await asyncio.wait_for(work, 2)
            result = self.store.get(task['id'])
            self.assertEqual(result['status'], 'RETRY_WAIT')
            self.assertEqual(result['error_code'], 'AGENT_CHANGED')
            self.assertEqual((result['user_id'],result['chat_id'],result['priority']),(42,-100,1))
            self.assertFalse(self.coordinator.books)
            self.assertFalse(self.coordinator.jobs)
            self.assertFalse(self.pool.active_tasks)
        finally:
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)

    async def test_restart_uses_received_pages_and_new_lease(self):
        task = self.store.submit(manga(1), 1, 1)
        self.coordinator.touch('vps-1')
        work = asyncio.create_task(self.pool.workers[0].run_once())
        job = None
        for _ in range(100):
            job = self.coordinator.claim('vps-1')
            if job: break
            await asyncio.sleep(0.01)
        self.assertIsNotNone(job)
        record = self.coordinator.jobs[job['id']]
        tmp = record.stage / 'test.part'
        tmp.write_bytes(image_bytes())
        self.coordinator.accept(job['id'], 'vps-1', job['lease'], 1, tmp, '.png')
        work.cancel()
        await asyncio.gather(work, return_exceptions=True)
        self.assertFalse(self.coordinator.jobs)
        self.store.recover()
        work = asyncio.create_task(self.pool.workers[0].run_once())
        try:
            recovered = None
            for _ in range(100):
                recovered = self.coordinator.claim('vps-1')
                if recovered: break
                await asyncio.sleep(0.01)
            self.assertIsNotNone(recovered)
            self.assertNotEqual(job['lease'], recovered['lease'])
            self.assertNotIn(1, [p['number'] for p in recovered['pages']])
            self.assertEqual(recovered['book']['task_id'], task['id'])
        finally:
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)

    async def test_failed_book_requeues_to_another_vps_with_original_owner(self):
        task = self.store.submit(manga(1), 42, -100)
        self.coordinator.touch('vps-1')
        work = asyncio.create_task(self.pool.workers[0].run_once())
        following = None
        try:
            job = None
            for _ in range(100):
                job = self.coordinator.claim('vps-1')
                if job: break
                await asyncio.sleep(0.01)
            self.assertIsNotNone(job)
            record = self.coordinator.jobs[job['id']]
            tmp = record.stage / 'test.part'
            tmp.write_bytes(image_bytes())
            self.coordinator.accept(job['id'], 'vps-1', job['lease'], 1, tmp, '.png')
            record.expires = 0
            await asyncio.wait_for(work, 2)
            self.assertEqual(self.store.get(task['id'])['status'], 'RETRY_WAIT')
            self.store.update(task['id'], next_at=0)
            self.coordinator.touch('vps-2')
            following = asyncio.create_task(self.pool.workers[1].run_once())
            seen = await asyncio.wait_for(self.pump('vps-2', following, FakeProvider()), 5)
            await following
            self.assertEqual(seen, ['1', '1'])
            result = self.store.get(task['id'])
            self.assertEqual(result['status'], 'COMPLETED')
            self.assertEqual((result['user_id'], result['chat_id']), (42, -100))
            with self.assertRaises(LeaseConflict):
                self.coordinator.heartbeat(job['id'], 'vps-1', job['lease'])
        finally:
            for operation in (work, following):
                if operation:
                    operation.cancel()
                    await asyncio.gather(operation, return_exceptions=True)


HAS_AIOHTTP = importlib.util.find_spec('aiohttp') is not None


@unittest.skipUnless(HAS_AIOHTTP, 'aiohttp required for HTTP integration tests')
class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_http_agent_upload_round_trip(self):
        from aiohttp import ClientSession
        from aiohttp.test_utils import TestClient, TestServer
        from jmemby.distributed.server import create_app
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stage = root / 'master'
            stage.mkdir()
            coordinator = Coordinator(MasterSettings(True, TOKENS))
            coordinator.touch('vps-1')
            coordinator.assign('vps-1', {'id': 'task'}, manga(1))
            pages = [Page(i, f'jm:10:{i-1}') for i in range(1, 18)]
            work = asyncio.create_task(coordinator.download_batch('vps-1', pages, stage, 100000, 10000, 100000))
            await asyncio.sleep(0)
            client = TestClient(TestServer(create_app(coordinator)))
            await client.start_server()
            settings = AgentSettings(str(client.make_url('/')).rstrip('/'), 'vps-1', TOKENS['vps-1'],
                                     work_dir=str(root / 'agent'), min_free_space_mb=0, max_page_bytes=10000)
            agent = Agent(settings, FakeProvider())
            headers = {'X-Agent-ID': 'vps-1', 'X-Agent-Instance': 'a' * 32,
                       'Authorization': 'Bearer ' + TOKENS['vps-1']}
            try:
                async with ClientSession(headers=headers) as session:
                    job = (await agent.request(session, 'POST', 'claim', json={}))['job']
                    await asyncio.wait_for(agent.process(session, job), 5)
                    await asyncio.wait_for(work, 1)
                self.assertEqual(len(list(stage.glob('*.png'))), 17)
                self.assertFalse(list((root / 'agent').glob('chapter-*')))
            finally:
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
                await client.close()

    async def test_auth_upload_integrity_and_stale_lease(self):
        from aiohttp.test_utils import TestClient, TestServer
        from jmemby.distributed.server import create_app
        with tempfile.TemporaryDirectory() as directory:
            coordinator = Coordinator(MasterSettings(True, TOKENS))
            coordinator.touch('vps-1')
            coordinator.assign('vps-1', {'id': 'task'}, manga(1))
            work = asyncio.create_task(coordinator.download_batch('vps-1', [Page(1, 'jm:10:0')],
                                       Path(directory), 100000, 10000, 100000))
            await asyncio.sleep(0)
            client = TestClient(TestServer(create_app(coordinator)))
            await client.start_server()
            try:
                headers = {'X-Agent-ID': 'vps-1', 'Authorization': 'Bearer ' + TOKENS['vps-1'],
                           'X-Agent-Instance': 'a' * 32}
                self.assertEqual((await client.post('/v1/agent/claim', json={})).status, 401)
                result = await client.post('/v1/agent/claim', json={}, headers=headers)
                job = (await result.json())['job']
                headers['X-Lease'] = job['lease']
                url = '/v1/agent/jobs/' + job['id'] + '/pages/1'
                payload = image_bytes()
                headers['X-Content-SHA256'] = '0' * 64
                self.assertEqual((await client.put(url, data=payload, headers=headers)).status, 400)
                headers['X-Content-SHA256'] = hashlib.sha256(payload).hexdigest()
                invalid = b'not an image'
                invalid_headers = {**headers, 'X-Content-SHA256': hashlib.sha256(invalid).hexdigest()}
                self.assertEqual((await client.put(url, data=invalid, headers=invalid_headers)).status, 422)
                oversized = b'x' * 100001
                self.assertEqual((await client.put(url, data=oversized, headers=headers)).status, 413)
                self.assertEqual((await client.put(url + '999', data=payload, headers=headers)).status, 409)
                self.assertEqual((await client.put(url, data=payload, headers=headers)).status, 200)
                self.assertEqual((await client.put(url, data=payload, headers=headers)).status, 200)
                wrong = {**headers, 'X-Agent-ID': 'vps-2', 'Authorization': 'Bearer ' + TOKENS['vps-2']}
                self.assertEqual((await client.put(url, data=payload, headers=wrong)).status, 409)
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
                self.assertEqual((await client.put(url, data=payload, headers=headers)).status, 409)
            finally:
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
                await client.close()
