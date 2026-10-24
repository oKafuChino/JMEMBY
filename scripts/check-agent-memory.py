"""Offline CI smoke: real SDK decoding and HTTP upload inside a 128 MiB cgroup.

Run after check-agent-platform.sh with the packaged Agent on PYTHONPATH.
No JM requests, enrollment, Telegram messages or real manga are involved.
"""
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import tempfile


def fixtures(directory):
    # Encode before applying the memory cap: production downloads encoded data.
    from PIL import Image
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    for suffix, width in [('jpg', 1500), ('webp', 1500), ('jpg', 1501)]:
        with Image.new('RGB', (width, 2000), '#4582ad') as generated:
            generated.save(root / f'{width}.{suffix}')


def provider(fixture):
    # Use the real SDK decoder; only the source HTTP client is replaced.
    from jmcomic import JmImageTool
    from jmemby.providers import jm_adapter

    class OfflineClient:
        def get_photo_detail(self, chapter):
            return [fixture] * 6

        def download_by_image_detail(self, image, destination):
            with JmImageTool.open_image(Path(image).read_bytes()) as decoded:
                JmImageTool.decode_and_save(10, decoded, destination)

    jm_adapter.initialize_client = OfflineClient
    jm_adapter.main()


async def smoke():
    from aiohttp import ClientSession, ClientTimeout, web
    from jmemby.distributed.agent import Agent, failure_code
    from jmemby.distributed.config import AgentSettings
    from jmemby.providers.builtin import JmProvider

    uploaded, failures, completed = [], [], []

    async def receive(request):
        assert request.headers['X-Agent-ID'] == 'memory-smoke'
        assert request.headers['Authorization'] == 'Bearer ' + 'a' * 43
        assert request.headers['X-Lease'] == 'b' * 43
        if request.method == 'PUT':
            digest, size = hashlib.sha256(), 0
            async for part in request.content.iter_chunked(65536):
                digest.update(part)
                size += len(part)
            assert digest.hexdigest() == request.headers['X-Content-SHA256']
            assert 0 < size <= 8 * 1024**2
            uploaded.append(int(request.path.rsplit('/', 1)[1]))
        elif request.path.endswith('/fail'):
            failures.append((await request.json())['code'])
        elif request.path.endswith('/complete'):
            completed.append(True)
        return web.json_response({'ok': True})

    app = web.Application()
    app.router.add_route('*', '/v1/agent/{path:.*}', receive)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 18081)
    try:
        await site.start()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = AgentSettings('http://127.0.0.1:18081', 'memory-smoke', 'a' * 43,
                                     work_dir=str(root / 'work'))
            agent = Agent(settings)
            assert agent.low_memory and 1 <= agent.memory_mb <= 128, agent.memory_mb
            job = {'id': 'a' * 32, 'lease': 'b' * 43, 'lease_seconds': 90,
                   'pages': [{'number': n, 'url': f'jm:52:{n-1}'} for n in range(1, 7)],
                   'max_bytes': 30 * 1024**2, 'max_pixels': 50_000_000,
                   'budget': 100_000_000, 'concurrency': 16}
            async with ClientSession(timeout=ClientTimeout(total=30), headers={
                    'X-Agent-ID': 'memory-smoke', 'Authorization': 'Bearer ' + 'a' * 43}) as session:
                for suffix, width in [('jpg', 1500), ('webp', 1500), ('jpg', 1501)]:
                    fixture = Path('/tmp/agent-memory-fixtures') / f'{width}.{suffix}'
                    agent.provider = JmProvider(json.dumps([
                        sys.executable, str(Path(__file__).resolve()), '--provider', str(fixture)]), 60)
                    uploaded.clear()
                    if width == 1500:
                        await agent.process(session, job)
                        assert uploaded == list(range(1, 7)), uploaded
                    else:
                        try:
                            await agent.process(session, job)
                        except ExceptionGroup as exc:
                            assert failure_code(exc) == 'IMAGE_LIMIT', failure_code(exc)
                        else:
                            raise AssertionError('Oversized page was not rejected')
                        assert not uploaded
                    assert not list(agent.root.iterdir()), 'Temporary pages leaked'
            assert len(completed) == 2 and failures == ['IMAGE_LIMIT']
            print('128 MiB smoke passed: JPEG/WebP decode, subprocesses, HTTP uploads and image rejection.')
    finally:
        await runner.cleanup()


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--provider':
        provider(sys.argv[2])
    elif len(sys.argv) > 1 and sys.argv[1] == '--fixtures':
        fixtures(sys.argv[2])
    else:
        asyncio.run(smoke())
