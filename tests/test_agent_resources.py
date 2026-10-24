import asyncio
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from PIL import Image

from jmemby.distributed.agent import Agent, failure_code
from jmemby.distributed.config import AgentSettings
from jmemby.distributed.resources import memory_limit_mb, use_low_memory
from jmemby.domain.errors import JobError
from jmemby.providers.command import CommandProvider


class MemoryProbeTests(unittest.TestCase):
    def test_physical_and_cgroup_limits(self):
        cases = [
            ({'proc/meminfo': 'MemTotal: 131072 kB'}, 128),
            ({}, None),
            ({'proc/meminfo': 'MemTotal: invalid kB', 'cg/memory.max': 'max'}, None),
            ({'proc/meminfo': 'MemTotal: 8388608 kB', 'cg/memory.max': str(128 * 1024**2)}, 128),
            ({'proc/self/cgroup': '0::/parent/child', 'cg/memory.max': 'max',
              'cg/parent/memory.max': str(128 * 1024**2), 'cg/parent/child/memory.max': 'max'}, 128),
            ({'proc/self/cgroup': '4:cpu,memory:/parent/child',
              'cg/memory/parent/memory.limit_in_bytes': str(64 * 1024**2),
              'cg/memory/parent/child/memory.limit_in_bytes': str(128 * 1024**2)}, 64),
            ({'proc/meminfo': 'MemTotal: 131072 kB',
              'cg/memory/memory.limit_in_bytes': str(2**63 - 4096)}, 128),
            ({'proc/self/cgroup': '0::/../outside', 'outside/memory.max': '1'}, None),
        ]
        for files, expected in cases:
            with self.subTest(files=files), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                for name, content in files.items():
                    path = root / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(content)
                self.assertEqual(memory_limit_mb(root / 'proc', root / 'cg'), expected)

    def test_mode_and_explicit_disk_reserve(self):
        self.assertTrue(use_low_memory('auto', 256))
        self.assertFalse(use_low_memory('auto', 257))
        self.assertFalse(use_low_memory('auto', None))
        self.assertTrue(use_low_memory('on', None))
        self.assertFalse(use_low_memory('off', 128))
        env = {'AGENT_MASTER_URL': 'https://example.test', 'AGENT_ID': 'test', 'AGENT_TOKEN': 'a' * 43}
        with patch.dict(os.environ, env, clear=True):
            settings = AgentSettings.from_env()
            self.assertEqual(settings.low_memory, 'auto')
            self.assertIsNone(settings.min_free_space_mb)
            with patch.dict(os.environ, {'AGENT_LOW_MEMORY': 'on', 'AGENT_MIN_FREE_SPACE_MB': '512'}):
                self.assertEqual(AgentSettings.from_env().min_free_space_mb, 512)
                self.assertEqual(AgentSettings.from_env().low_memory, 'on')
            with patch.dict(os.environ, {'AGENT_LOW_MEMORY': 'invalid'}):
                with self.assertRaises(ValueError):
                    AgentSettings.from_env()


class AgentResourceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        stream = io.BytesIO()
        Image.new('RGB', (8, 8), 'white').save(stream, 'PNG')
        self.image = stream.getvalue()
        self.calls, self.uploads, self.events = [], [], []
        self.workers = set()

    def agent(self, mode='auto', memory=128, reserve=None):
        settings = AgentSettings('https://example.test', 'test', 'a' * 43,
                                 work_dir=str(self.root), low_memory=mode, min_free_space_mb=reserve)
        async def download(pages, stage, max_bytes, max_pixels, budget):
            self.workers.add(asyncio.current_task())
            self.calls.append((len(pages), max_bytes, max_pixels, budget))
            await asyncio.sleep(0)
            for page in pages:
                (stage / f'{page.number:06d}.png').write_bytes(self.image)
        provider = SimpleNamespace(download_batch=AsyncMock(side_effect=download))
        with patch('jmemby.distributed.agent.memory_limit_mb', return_value=memory):
            agent = Agent(settings, provider)
        async def request(session, method, path, **kwargs):
            self.events.append((path, kwargs.get('json')))
            if method == 'PUT':
                data = kwargs['data'].read()
                self.assertEqual(data, self.image)
                self.assertEqual(kwargs['headers']['X-Content-SHA256'], hashlib.sha256(data).hexdigest())
                self.uploads.append(int(path.rsplit('/', 1)[1]))
            return {'ok': True}
        agent.request = AsyncMock(side_effect=request)
        return agent

    def job(self, count=35, concurrency=16):
        return {'id': 'a' * 32, 'lease': 'b' * 43, 'lease_seconds': 90,
                'pages': [{'number': n, 'url': f'jm:52:{n-1}'} for n in range(1, count + 1)],
                'max_bytes': 30 * 1024**2, 'max_pixels': 50_000_000,
                'budget': 100_000_000, 'concurrency': concurrency}

    async def test_small_node_caps_controller_parallelism_and_reuses_worker(self):
        agent = self.agent()
        # Enough for 64 MiB reserve + a single 8 MiB page, but not the old 512 MiB reserve.
        with patch('jmemby.distributed.agent.shutil.disk_usage', return_value=SimpleNamespace(free=80 * 1024**2)):
            await agent.process(None, self.job(100))
        self.assertTrue(agent.low_memory)
        self.assertEqual(self.uploads, list(range(1, 101)))
        self.assertEqual(len(self.workers), 1)
        self.assertTrue(all(call == (1, 8 * 1024**2, 3_000_000, 8 * 1024**2) for call in self.calls))
        self.assertTrue(any(path.endswith('/heartbeat') for path, _ in self.events))
        self.assertTrue(self.events[-1][0].endswith('/complete'))
        self.assertEqual(list(self.root.iterdir()), [])

    async def test_normal_node_retains_parallel_groups_with_bounded_workers(self):
        agent = self.agent(memory=1024, reserve=0)
        await agent.process(None, self.job(35, concurrency=2))
        self.assertFalse(agent.low_memory)
        self.assertEqual([call[0] for call in self.calls], [8, 8, 8, 8, 3])
        self.assertEqual(len(self.workers), 2)
        self.assertEqual(sorted(self.uploads), list(range(1, 36)))

    async def test_stricter_controller_limits_remain_effective(self):
        agent = self.agent(reserve=0)
        job = self.job(1)
        job.update(max_bytes=10000, max_pixels=1000)
        await agent.process(None, job)
        self.assertEqual(self.calls, [(1, 10000, 1000, 10000)])

    async def test_explicit_reserve_is_preserved_and_failure_reported(self):
        agent = self.agent(reserve=512)
        with patch('jmemby.distributed.agent.shutil.disk_usage', return_value=SimpleNamespace(free=80 * 1024**2)):
            with self.assertRaises(JobError) as caught:
                await agent.process(None, self.job())
        self.assertEqual(caught.exception.code, 'DISK_LOW')
        self.assertFalse(self.calls)
        self.assertEqual(self.events[-1][1], {'code': 'DISK_LOW'})

    async def test_nested_failure_cleans_stage_and_reports_safe_code(self):
        agent = self.agent(reserve=0)
        agent.provider.download_batch.side_effect = JobError('MEMORY_LIMIT', 'private details')
        with self.assertRaises(ExceptionGroup):
            await agent.process(None, self.job())
        self.assertEqual(self.events[-1][1], {'code': 'MEMORY_LIMIT'})
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertNotIn('private details', str(self.events))

    async def test_budget_still_applies_across_single_page_batches(self):
        agent = self.agent(reserve=0)
        job = self.job(3)
        job['budget'] = len(self.image)
        with self.assertRaises(ExceptionGroup) as caught:
            await agent.process(None, job)
        self.assertEqual(failure_code(caught.exception), 'SIZE_LIMIT')
        self.assertEqual(self.uploads, [1])
        self.assertEqual(list(self.root.iterdir()), [])

    async def test_cancellation_drains_worker_and_removes_partial_files(self):
        agent = self.agent(reserve=0)
        started, drained = asyncio.Event(), asyncio.Event()
        async def download(pages, stage, *limits):
            (stage / 'partial.part').write_bytes(b'partial')
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                drained.set()
        agent.provider.download_batch.side_effect = download
        work = asyncio.create_task(agent.process(None, self.job()))
        try:
            await asyncio.wait_for(started.wait(), 1)
            work.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(work, 1)
            self.assertTrue(drained.is_set())
            self.assertEqual(list(self.root.iterdir()), [])
            self.assertFalse(any(path.endswith(('/fail', '/complete')) for path, _ in self.events))
        finally:
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)

    def test_failure_code_filters_nested_arbitrary_messages(self):
        error = ExceptionGroup('private', [ValueError('secret'), ExceptionGroup('inner', [JobError('IMAGE_LIMIT', 'secret')])])
        self.assertEqual(failure_code(error), 'IMAGE_LIMIT')
        self.assertEqual(failure_code(JobError('secret token!', 'secret')), 'AGENT_FAILED')

    async def test_killed_provider_has_specific_error(self):
        process = SimpleNamespace(stdin=SimpleNamespace(write=Mock(), drain=AsyncMock(), close=Mock()),
                                  stdout=SimpleNamespace(read=AsyncMock(return_value=b'')),
                                  wait=AsyncMock(), returncode=-9)
        provider = CommandProvider(json.dumps(['unused']))
        with patch('jmemby.providers.command.asyncio.create_subprocess_exec', return_value=process), \
                patch('jmemby.providers.command.os.name', 'posix'), \
                patch('jmemby.providers.command.signal.SIGKILL', 9, create=True):
            with self.assertRaises(JobError) as caught:
                await provider.request({})
        self.assertEqual(caught.exception.code, 'PROVIDER_KILLED')
        self.assertTrue(caught.exception.retryable)
