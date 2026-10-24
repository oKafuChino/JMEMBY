import asyncio
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from PIL import Image

from jmemby.distributed.agent import Agent
from jmemby.distributed.config import AgentSettings
from jmemby.domain.errors import JobError
from jmemby.domain.models import Page
from jmemby.providers.image_decoder import decode
from jmemby.providers.jm_adapter import download_raw, classify_error, handle, make_client
from jmemby.providers.low_memory import LowMemoryJmProvider


class RawTransferTests(unittest.TestCase):
    def test_redirect_is_closed_without_buffering_and_downgrade_is_rejected(self):
        module = SimpleNamespace(JmImageTool=SimpleNamespace(get_num_by_url=lambda *args: 10),
                                 JmModuleConfig=SimpleNamespace(new_html_headers=lambda: {}))
        image = SimpleNamespace(download_url='https://example.test/52/1.jpg', scramble_id='1')
        for location, allowed in [('https://cdn.test/image.jpg', True), ('http://cdn.test/image.jpg', False)]:
            with self.subTest(location=location), tempfile.TemporaryDirectory() as directory:
                redirect = SimpleNamespace(status_code=302, headers={'Location': location}, close=Mock())
                response = SimpleNamespace(status_code=200, headers={}, close=Mock(), raise_for_status=Mock(),
                                           iter_content=Mock(return_value=[b'abc']))
                client = SimpleNamespace(postman=SimpleNamespace(get=Mock(side_effect=[redirect, response])),
                                         update_request_with_specify_domain=Mock())
                with patch.dict(sys.modules, jmcomic=module):
                    if allowed:
                        download_raw(client, image, Path(directory) / 'raw', 10)
                        response.close.assert_called_once()
                        self.assertEqual(client.postman.get.call_args.args[0], location)
                    else:
                        with self.assertRaises(JobError):
                            download_raw(client, image, Path(directory) / 'raw', 10)
                        self.assertEqual(client.postman.get.call_count, 1)
                redirect.close.assert_called_once()

    def test_stream_limits_close_response_and_never_read_content(self):
        class Response:
            status_code = 200
            def __init__(self, chunks, headers):
                self.headers, self.chunks = headers, chunks
                self.closed, self.reads = False, 0
            @property
            def content(self):
                raise AssertionError('Unbounded response read')
            def raise_for_status(self):
                pass
            def iter_content(self, chunk_size):
                self.chunk_size = chunk_size
                for chunk in self.chunks:
                    self.reads += 1
                    yield chunk
            def close(self):
                self.closed = True
        module = SimpleNamespace(JmImageTool=SimpleNamespace(get_num_by_url=Mock(return_value=10)),
                                 JmModuleConfig=SimpleNamespace(new_html_headers=lambda: {}))
        image = SimpleNamespace(download_url='https://example.test/52/1.jpg?private', scramble_id='1')
        for chunks, headers, expected, reads in [
                ([b'abc', b'def'], {}, None, 2),
                ([b'abc', b'def', b'x', b'never'], {}, 'SIZE_LIMIT', 3),
                ([b'never'], {'Content-Length': '7'}, 'SIZE_LIMIT', 0),
                ([], {}, 'BAD_IMAGE', 0)]:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as directory:
                response = Response(chunks, headers)
                client = SimpleNamespace(postman=SimpleNamespace(get=Mock(return_value=response)),
                                         update_request_with_specify_domain=Mock())
                destination = Path(directory) / 'raw'
                with patch.dict(sys.modules, jmcomic=module):
                    if expected:
                        with self.assertRaises(JobError) as caught:
                            download_raw(client, image, destination, 6)
                        self.assertEqual(classify_error(caught.exception, 'image_transfer')['error']['code'], expected)
                        self.assertFalse(destination.exists())
                    else:
                        self.assertEqual(download_raw(client, image, destination, 6), {'segments': 10})
                        self.assertEqual(destination.read_bytes(), b'abcdef')
                self.assertTrue(response.closed)
                self.assertEqual(response.reads, reads)
                self.assertTrue(client.postman.get.call_args.kwargs['stream'])
                self.assertFalse(client.postman.get.call_args.kwargs['allow_redirects'])
                client.update_request_with_specify_domain.assert_called_once()
        self.assertEqual(module.JmImageTool.get_num_by_url.call_args.args, (1, 'https://example.test/52/1.jpg'))

    def test_raw_operation_never_uses_sdk_decoder(self):
        client = SimpleNamespace(get_photo_detail=Mock(return_value=['page']), download_by_image_detail=Mock())
        with patch('jmemby.providers.jm_adapter.download_raw', return_value={'segments': 10}) as raw:
            result = handle({'operation': 'download_raw', 'url': 'jm:52:0',
                             'destination': 'raw', 'max_bytes': 100}, client)
        self.assertEqual(result, {'segments': 10})
        raw.assert_called_once_with(client, 'page', Path('raw'), 100)
        client.download_by_image_detail.assert_not_called()

    def test_low_memory_client_uses_pull_based_transport(self):
        metadata = {'impersonate': 'chrome'}
        option = SimpleNamespace(client=SimpleNamespace(postman=SimpleNamespace(
            meta_data=SimpleNamespace(src_dict=metadata))), new_jm_client=Mock(return_value='client'))
        factory = Mock(return_value=option)
        with patch.dict(sys.modules, jmcomic=SimpleNamespace(JmOption=SimpleNamespace(construct=factory))):
            self.assertEqual(make_client(low_memory=True), 'client')
        self.assertEqual(factory.call_args.args[0]['client']['postman']['type'], 'requests')
        self.assertNotIn('impersonate', metadata)


class DecoderTests(unittest.TestCase):
    def test_restoration_matches_expected_pixels_with_uneven_strips(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for suffix in ('png', 'jpg', 'webp'):
                for segments in (0, 2, 10, 16, 20):
                    with self.subTest(suffix=suffix, segments=segments):
                        source, result, expected = root / ('raw.' + suffix), root / 'out.jpg', root / 'expected.jpg'
                        with Image.new('RGB', (25, 43)) as original:
                            for y in range(original.height):
                                for x in range(original.width):
                                    original.putpixel((x, y), ((y * 7) % 256, x * 9, y * 5))
                            original.save(source)
                        # Build the expected row mapping separately; JPEG output
                        # is compared byte-for-byte using identical encoding settings.
                        with Image.open(source) as original, Image.new('RGB', original.size) as restored:
                            order = list(range(original.height))
                            if segments:
                                step = original.height // segments
                                strips = [order[n * step:(n + 1) * step] for n in range(segments - 1)]
                                strips.append(order[(segments - 1) * step:])
                                order = [row for strip in reversed(strips) for row in strip]
                            for y, original_y in enumerate(order):
                                for x in range(original.width):
                                    restored.putpixel((x, y), original.getpixel((x, original_y)))
                            restored.save(expected, 'JPEG')
                        decode({'source': str(source), 'destination': str(result), 'segments': segments,
                                'max_bytes': 100000, 'max_pixels': 2000, 'budget': 100000})
                        self.assertEqual(result.read_bytes(), expected.read_bytes())

    def test_rejects_pixel_limit_budget_and_corruption_without_publishing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, destination = root / 'raw', root / 'out.jpg'
            with Image.new('RGB', (40, 40)) as picture:
                picture.save(source, 'PNG')
            request = {'source': str(source), 'destination': str(destination), 'segments': 10,
                       'max_bytes': 10000, 'max_pixels': 1000, 'budget': 10000}
            old_limit = Image.MAX_IMAGE_PIXELS
            with self.assertRaises(Image.DecompressionBombWarning):
                decode(request)
            self.assertEqual(Image.MAX_IMAGE_PIXELS, old_limit)
            self.assertFalse(destination.exists())
            request.update(max_pixels=2000, budget=1)
            with self.assertRaises(JobError) as caught:
                decode(request)
            self.assertEqual(caught.exception.code, 'SIZE_LIMIT')
            self.assertFalse(destination.exists())
            source.write_bytes(b'broken image')
            with self.assertRaises(OSError):
                decode(request)
            self.assertFalse(destination.exists())

    def test_agent_import_does_not_load_pillow_or_sdk(self):
        result = subprocess.run([sys.executable, '-c',
            "import sys; import jmemby.distributed.agent; "
            "assert not any(m == 'PIL' or m.startswith('PIL.') or m == 'jmcomic' "
            "or m.startswith('jmcomic.') for m in sys.modules)"],
            env={**os.environ, 'PYTHONPATH': str(Path(__file__).parents[1] / 'src')},
            capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_decoder_subprocess_and_parent_skip_duplicate_decode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = AgentSettings('https://example.test', 'test', 'a' * 43,
                                     work_dir=str(root), low_memory='on', min_free_space_mb=0)
            agent = Agent(settings)
            self.assertIsInstance(agent.provider, LowMemoryJmProvider)
            async def transfer(request):
                with Image.new('RGB', (32, 43), 'white') as picture:
                    picture.save(request['destination'], 'PNG')
                return {'segments': 10}
            agent.provider.request = AsyncMock(side_effect=transfer)
            uploaded = []
            async def request(session, method, path, **kwargs):
                if method == 'PUT':
                    with Image.open(io.BytesIO(kwargs['data'].read())) as picture:
                        self.assertEqual(picture.size, (32, 43))
                        picture.load()
                    uploaded.append(path)
                return {}
            agent.request = request
            with patch('jmemby.distributed.agent.validate_image', side_effect=AssertionError('Parent decoded pixels')):
                await agent.process(None, {'id': 'a' * 32, 'lease': 'b' * 43, 'lease_seconds': 90,
                    'pages': [{'number': n, 'url': f'jm:52:{n-1}'} for n in range(1, 4)],
                    'max_bytes': 10000, 'max_pixels': 2000, 'budget': 10000, 'concurrency': 16})
            self.assertEqual(len(uploaded), 3)
            self.assertEqual(list(root.iterdir()), [])

    async def test_cancel_during_transfer_removes_raw_file_and_does_not_decode(self):
        with tempfile.TemporaryDirectory() as directory:
            root, started = Path(directory), asyncio.Event()
            provider = LowMemoryJmProvider(json.dumps(['unused']))
            async def transfer(request):
                Path(request['destination']).write_bytes(b'partial')
                started.set()
                await asyncio.Event().wait()
            provider.request = transfer
            with patch('jmemby.providers.low_memory.CommandProvider.request', new_callable=AsyncMock) as decoder:
                work = asyncio.create_task(provider.download_batch([Page(1, 'jm:52:0')], root, 1000, 1000, 1000))
                await asyncio.wait_for(started.wait(), 1)
                work.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await work
                decoder.assert_not_called()
            self.assertEqual(list(root.iterdir()), [])

    async def test_cancel_during_decode_drains_worker_before_cleaning_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root, started, drained = Path(directory), asyncio.Event(), asyncio.Event()
            provider = LowMemoryJmProvider(json.dumps(['unused']))
            async def transfer(request):
                Path(request['destination']).write_bytes(b'source')
                return {'segments': 10}
            async def decoding(request):
                Path(request['destination']).write_bytes(b'partial')
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    self.assertTrue(Path(request['source']).exists())
                    drained.set()
            provider.request = transfer
            with patch('jmemby.providers.low_memory.CommandProvider.request', new_callable=AsyncMock,
                       side_effect=decoding):
                work = asyncio.create_task(provider.download_batch([Page(1, 'jm:52:0')], root, 1000, 1000, 1000))
                await asyncio.wait_for(started.wait(), 1)
                work.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await work
            self.assertTrue(drained.is_set())
            self.assertEqual(list(root.iterdir()), [])
