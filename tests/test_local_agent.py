import asyncio
import hashlib
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from PIL import Image

from jmemby.config import Settings
from jmemby.db.store import Store
from jmemby.distributed.agent import Rejected
from jmemby.distributed.config import MasterSettings
from jmemby.distributed.coordinator import Coordinator
from jmemby.distributed.local import LocalAgent
from jmemby.distributed.nodes import LOCAL_NODE_ID, NodeRegistry
from jmemby.distributed.workers import WorkerPool
from jmemby.domain.errors import JobError
from jmemby.domain.models import Chapter, Manga, Page
from jmemby.services.users import UserService


def manga(number):
    return Manga('jmcomic', str(number), f'Book {number}', (), (
        Chapter(str(number * 10), 1, 'First', ()), Chapter(str(number * 10 + 1), 2, 'Second', ())))


class Provider:
    source = 'jmcomic'

    def __init__(self):
        image = io.BytesIO()
        Image.new('RGB', (8, 8)).save(image, 'PNG')
        self.content = image.getvalue()
        self.books = []

    async def get_pages(self, chapter):
        return tuple(Page(i, f'jm:{chapter}:{i-1}') for i in range(1, 4))

    async def download_batch(self, pages, stage, *limits):
        self.books.append(pages[0].url.split(':')[1])
        for page in pages:
            (stage / f'{page.number:06d}.png').write_bytes(self.content)


class LocalAgentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'db')
        self.settings = Settings('token', frozenset({1}), str(self.root / 'db'), str(self.root / 'storage'),
                                 'http://komga', '', 'library', '/data', max_page_bytes=10000, max_task_bytes=1000000)
        self.registry = NodeRegistry(self.store)
        self.registry.ensure_local()
        self.coordinator = Coordinator(MasterSettings())
        self.coordinator.registry = self.registry
        self.provider = Provider()
        self.agent = LocalAgent(self.settings, self.coordinator, self.provider)
        async def imported(paths):
            return {path: str(i) for i, path in enumerate(paths)}
        self.pool = WorkerPool(self.store, self.provider, SimpleNamespace(import_paths=imported),
                               self.settings, self.coordinator)
        self.operations = []

    async def asyncTearDown(self):
        for operation in self.operations:
            operation.cancel()
        await asyncio.gather(*self.operations, return_exceptions=True)
        self.store.close()
        self.temp.cleanup()

    def background(self, coroutine):
        operation = asyncio.create_task(coroutine)
        self.operations.append(operation)
        return operation

    async def pump(self, operation):
        async with asyncio.timeout(5):
            while not operation.done():
                job = (await self.agent.request(None, 'POST', 'claim'))['job']
                if job:
                    await self.agent.process(None, job)
                else:
                    await asyncio.sleep(0.01)
        await operation

    def test_default_node_and_persistent_settings(self):
        self.assertEqual(self.registry.ids(), [LOCAL_NODE_ID])
        self.assertEqual(self.registry.origin(), '')
        public = self.registry.public(LOCAL_NODE_ID)
        self.assertEqual((public['label'], public['concurrency'], public['queue_scope']), ('主控节点', 1, 'all'))
        self.assertEqual((public['kind'], public['registered'], public['enabled']), ('local', True, 1))
        self.registry.update(LOCAL_NODE_ID, 'Local reader', 3, 'white')
        self.registry.set_enabled(LOCAL_NODE_ID, False)
        restarted = NodeRegistry(self.store)
        restarted.ensure_local()
        row = restarted.public(LOCAL_NODE_ID)
        self.assertEqual((row['label'], row['concurrency'], row['queue_scope'], row['enabled']), ('Local reader', 3, 'white', 0))

    def test_local_identity_cannot_be_installed_deleted_or_used_by_remote_agent(self):
        for operation in (lambda: self.registry.delete(LOCAL_NODE_ID),
                          lambda: self.registry.issue(LOCAL_NODE_ID),
                          lambda: self.registry.enroll(LOCAL_NODE_ID, 'a' * 43),
                          lambda: self.registry.create(LOCAL_NODE_ID, 'Fake', 1)):
            with self.assertRaises(ValueError):
                operation()
        self.assertFalse(self.coordinator.authenticate(LOCAL_NODE_ID, ''))
        self.assertFalse(self.coordinator.authenticate(LOCAL_NODE_ID, 'a' * 43))
        self.assertEqual(self.registry.get(LOCAL_NODE_ID)['token_hash'], '')

    def test_local_node_does_not_consume_remote_quota_and_sorts_first(self):
        for i in range(32):
            self.registry.create(f'remote-{i}', 'Remote', 1)
        self.assertEqual(len(self.registry.ids()), 33)
        self.assertEqual(self.registry.ids()[0], LOCAL_NODE_ID)
        with self.assertRaises(ValueError):
            self.registry.create('extra', 'Extra', 1)
        NodeRegistry(self.store).ensure_local()
        self.assertEqual(len(self.registry.ids()), 33)

    async def test_master_downloads_whole_book_without_remote_nodes_or_https(self):
        users = UserService(self.store, {1})
        users.register(2)
        users.grant(1, 3, white=True)
        users.register(3)
        normal = self.store.submit(manga(1), 2, 2)
        white = self.store.submit(manga(2), 3, 3)
        operation = self.background(self.pool.workers[0].run_once())
        await self.pump(operation)
        task = self.store.get(white['id'])
        self.assertEqual(task['status'], 'COMPLETED')
        self.assertEqual(task['assigned_node_id'], LOCAL_NODE_ID)
        self.assertEqual(task['assigned_node_label'], '主控节点')
        self.assertEqual((task['user_id'], task['chat_id'], task['priority']), (3, 3, 1))
        self.assertEqual(self.store.get(normal['id'])['status'], 'QUEUED')
        self.assertEqual(self.provider.books, ['20', '21'])
        self.assertEqual(len(list((self.root / 'storage/library').rglob('*.cbz'))), 2)
        self.assertFalse(self.coordinator.books)
        self.assertFalse(self.coordinator.jobs)
        self.assertFalse(self.pool.active_tasks)
        self.assertEqual(list(self.agent.root.iterdir()), [])
        self.assertTrue(self.coordinator.snapshot()['agents'][0]['online'])

    async def test_local_queue_scope_and_controller_concurrency(self):
        self.registry.update(LOCAL_NODE_ID, 'Local', 4, 'white')
        users = UserService(self.store, {1})
        users.register(2)
        normal = self.store.submit(manga(1), 2, 2)
        self.assertFalse(await self.pool.workers[0].run_once())
        self.assertEqual(self.store.get(normal['id'])['status'], 'QUEUED')
        self.registry.update(LOCAL_NODE_ID, 'Local', 4, 'normal')
        operation = self.background(self.pool.workers[0].run_once())
        await self.pump(operation)
        self.assertEqual(self.store.get(normal['id'])['status'], 'COMPLETED')

    async def test_master_and_remote_node_execute_different_books_together(self):
        self.registry.create('remote', 'Remote VPS', 1)
        self.coordinator.touch('remote')
        await self.agent.request(None, 'POST', 'claim')
        first = self.store.submit(manga(1), 1, 1)
        second = self.store.submit(manga(2), 1, 1)
        local_work = self.background(self.pool.workers[0].run_once())
        remote_work = self.background(self.pool.new_worker('remote').run_once())
        remote_books = []
        async def remote_transport():
            async with asyncio.timeout(5):
                while not remote_work.done():
                    job = self.coordinator.claim('remote')
                    if job:
                        remote_books.append(job['book']['source_id'])
                        for page in job['pages']:
                            temporary = self.root / 'remote-upload.part'
                            temporary.write_bytes(self.provider.content)
                            self.coordinator.accept(job['id'], 'remote', job['lease'], page['number'], temporary, '.png')
                        self.coordinator.finish(job['id'], 'remote', job['lease'])
                    await asyncio.sleep(0.01)
            await remote_work
        await asyncio.gather(self.pump(local_work), remote_transport())
        self.assertEqual(self.store.get(first['id'])['assigned_node_id'], LOCAL_NODE_ID)
        self.assertEqual(self.store.get(second['id'])['assigned_node_id'], 'remote')
        self.assertEqual(remote_books, ['2', '2'])
        self.assertEqual(self.provider.books, ['10', '11'])
        self.assertEqual([self.store.get(t['id'])['status'] for t in (first, second)], ['COMPLETED', 'COMPLETED'])
        self.assertFalse(self.pool.active_tasks)

    async def test_disabling_active_master_requeues_with_original_owner(self):
        await self.agent.request(None, 'POST', 'claim')
        task = self.store.submit(manga(1), 1, 1)
        worker = self.background(self.pool.workers[0].run_once())
        async with asyncio.timeout(2):
            while not self.coordinator.jobs:
                await asyncio.sleep(0.01)
        self.registry.set_enabled(LOCAL_NODE_ID, False)
        self.coordinator.invalidate(LOCAL_NODE_ID)
        await asyncio.wait_for(worker, 2)
        result = self.store.get(task['id'])
        self.assertEqual(result['status'], 'RETRY_WAIT')
        self.assertEqual(result['error_code'], 'AGENT_CHANGED')
        self.assertEqual((result['user_id'], result['chat_id'], result['priority']), (1, 1, 0))
        self.assertFalse(self.coordinator.books)
        self.assertFalse(self.coordinator.jobs)

    async def test_local_probe_requires_live_acknowledgement(self):
        self.coordinator.PROBE_TIMEOUT = 0.05
        self.assertEqual((await self.coordinator.test_connection(LOCAL_NODE_ID))['status'], 'timeout')
        self.coordinator.PROBE_TIMEOUT = 1
        operation = self.background(self.coordinator.test_connection(LOCAL_NODE_ID))
        await asyncio.sleep(0)
        response = await self.agent.request(None, 'POST', 'claim')
        await self.agent.answer_probe(None, response)
        self.assertEqual((await operation)['status'], 'success')

    async def test_disabled_claim_resumes_and_shutdown_marks_offline(self):
        self.registry.set_enabled(LOCAL_NODE_ID, False)
        self.coordinator.invalidate(LOCAL_NODE_ID)
        claim = self.background(self.agent.request(None, 'POST', 'claim'))
        await asyncio.sleep(0.01)
        self.assertFalse(claim.done())
        self.registry.set_enabled(LOCAL_NODE_ID, True)
        self.coordinator.invalidate(LOCAL_NODE_ID)
        self.assertIsNone((await asyncio.wait_for(claim, 2))['job'])
        running = self.background(self.agent.run())
        await asyncio.sleep(0.01)
        self.assertTrue(self.coordinator.online(LOCAL_NODE_ID))
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        self.assertFalse(self.coordinator.online(LOCAL_NODE_ID))

    async def start_chunk(self):
        await self.agent.request(None, 'POST', 'claim')
        self.coordinator.assign(LOCAL_NODE_ID, {'id': 'task'}, manga(1))
        stage = self.root / 'stage'
        stage.mkdir()
        operation = self.background(self.coordinator.download_batch(
            LOCAL_NODE_ID, [Page(1, 'jm:10:0')], stage, 10000, 10000, 10000))
        await asyncio.sleep(0)
        job = (await self.agent.request(None, 'POST', 'claim'))['job']
        return operation, job, stage

    async def test_local_upload_checks_checksum_and_lease(self):
        operation, job, stage = await self.start_chunk()
        route = f"jobs/{job['id']}/pages/1"
        headers = {'X-Lease': job['lease'], 'X-Content-SHA256': '0' * 64}
        with self.assertRaises(JobError):
            await self.agent.request(None, 'PUT', route, headers=headers, data=io.BytesIO(self.provider.content))
        self.assertEqual(list(stage.iterdir()), [])
        headers['X-Content-SHA256'] = hashlib.sha256(self.provider.content).hexdigest()
        await self.agent.request(None, 'PUT', route, headers=headers, data=io.BytesIO(self.provider.content))
        self.assertTrue((stage / '000001.png').is_file())
        self.registry.set_enabled(LOCAL_NODE_ID, False)
        self.coordinator.invalidate(LOCAL_NODE_ID)
        with self.assertRaises(Rejected):
            await self.agent.request(None, 'PUT', route, headers=headers, data=io.BytesIO(self.provider.content))
        with self.assertRaises(JobError):
            await operation

    async def test_provider_failure_reaches_coordinator_and_cleans_local_files(self):
        operation, job, stage = await self.start_chunk()
        async def fail(*args):
            raise JobError('MEMORY_LIMIT', 'private provider details')
        self.provider.download_batch = fail
        with self.assertRaises(ExceptionGroup):
            await self.agent.process(None, job)
        with self.assertRaises(JobError) as caught:
            await operation
        self.assertEqual(caught.exception.code, 'AGENT_RESOURCE_LIMIT')
        self.assertNotIn('private provider details', str(caught.exception))
        self.assertEqual(list(stage.iterdir()), [])
        self.assertEqual(list(self.agent.root.iterdir()), [])
