"""Offline CI smoke: real SDK transfer, isolated decoding and HTTP uploads.

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
    from PIL import Image, ImageDraw
    from jmcomic import JmImageTool
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    for suffix, width in [('jpg', 1500), ('webp', 1500), ('jpg', 1501)]:
        with Image.new('RGB', (width, 2000), '#4582ad') as generated:
            draw = ImageDraw.Draw(generated)
            for y in range(0, 2000, 7):
                draw.rectangle((0, y, width - 1, y + 3), fill=(y % 256, (y * 3) % 256, 80))
            source = root / f'{width}.{suffix}'
            generated.save(source)
        expected = root / 'expected.jpg'
        with Image.open(source) as picture:
            JmImageTool.decode_and_save(10, picture, str(expected))
        (root / f'{width}.{suffix}.sha256').write_text(hashlib.sha256(expected.read_bytes()).hexdigest())
        expected.unlink()


def provider(fixture):
    # Real SDK construction, image headers and HTTP transport. Only metadata
    # lookup and startup discovery/cookies are offline to avoid external calls.
    from types import SimpleNamespace
    from jmcomic import JmModuleConfig
    from jmemby.providers import jm_adapter

    JmModuleConfig.FLAG_API_CLIENT_AUTO_UPDATE_DOMAIN = False
    JmModuleConfig.FLAG_API_CLIENT_REQUIRE_COOKIES = False
    def offline_client(low_memory=False):
        assert low_memory
        client = jm_adapter.make_client(low_memory=True)
        client.get_photo_detail = lambda chapter: [SimpleNamespace(
            download_url=f'http://127.0.0.1:18081/media/photos/52/{fixture}', scramble_id='1')] * 6
        return client
    jm_adapter.initialize_client = offline_client
    jm_adapter.main()


async def smoke():
    from aiohttp import ClientSession, ClientTimeout, web
    from jmemby.distributed.agent import Agent, failure_code
    from jmemby.distributed.config import AgentSettings
    from jmemby.providers.low_memory import LowMemoryJmProvider

    assert 'PIL' not in sys.modules and 'jmcomic' not in sys.modules
    # Reserve resident memory for services/kernel headroom missing from a bare
    # container. Production VPS total RAM is not exclusively available to Agent.
    headroom = bytearray(16 * 1024**2)
    headroom[::4096] = b'\x01' * (len(headroom) // 4096)

    uploaded, failures, completed = [], [], []
    expected_digest = None

    async def source(request):
        filename = request.match_info['filename']
        assert filename in {'1500.jpg', '1500.webp', '1501.jpg'}
        assert request.headers.get('X-Requested-With')
        # FileResponse streams from disk; the Agent must use the real SDK's
        # requests transport and must not materialize response.content.
        return web.FileResponse(Path('/tmp/agent-memory-fixtures') / filename)

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
            assert digest.hexdigest() == expected_digest, 'Descrambling differs from SDK'
            assert 0 < size <= 8 * 1024**2
            uploaded.append(int(request.path.rsplit('/', 1)[1]))
        elif request.path.endswith('/fail'):
            failures.append((await request.json())['code'])
        elif request.path.endswith('/complete'):
            completed.append(True)
        return web.json_response({'ok': True})

    app = web.Application()
    app.router.add_get('/media/photos/52/{filename}', source)
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
                for suffix, width in [('jpg', 1500), ('webp', 1500), ('jpg', 1500), ('jpg', 1501)]:
                    fixture = Path('/tmp/agent-memory-fixtures') / f'{width}.{suffix}'
                    expected_digest = fixture.with_suffix(f'.{suffix}.sha256').read_text().strip()
                    agent.provider = LowMemoryJmProvider(json.dumps([
                        sys.executable, str(Path(__file__).resolve()), '--provider', fixture.name]), 60)
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
                    assert 'PIL' not in sys.modules and 'jmcomic' not in sys.modules
            assert len(completed) == 3 and failures == ['IMAGE_LIMIT']
            assert len(headroom) == 16 * 1024**2
            print('128 MiB smoke passed with 16 MiB headroom: SDK streaming, isolated JPEG/WebP '
                  'decoding equals SDK output, repeated chapters, uploads and image rejection.')
    finally:
        await runner.cleanup()


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--provider':
        provider(sys.argv[2])
    elif len(sys.argv) > 1 and sys.argv[1] == '--fixtures':
        fixtures(sys.argv[2])
    else:
        asyncio.run(smoke())
