"""Serial SDK transfer and SDK-free decoding for memory-constrained Agents."""
import json
import logging
import sys

from jmemby.domain.errors import JobError
from jmemby.providers.builtin import JmProvider
from jmemby.providers.command import CommandProvider


class LowMemoryJmProvider(JmProvider):
    async def download_batch(self, pages, stage, max_bytes, max_pixels, budget):
        decoder = CommandProvider(json.dumps([
            sys.executable, '-m', 'jmemby.providers.image_decoder']), self.timeout)
        for page in pages:
            source = stage / f'.source-{page.number}'
            destination = stage / f'{page.number:06d}.jpg'
            phase = 'transfer'
            try:
                # request() waits for process exit, releasing SDK/TLS memory before
                # the image worker starts. Never overlap these two processes.
                result = await self.request({
                    'operation': 'download_raw', 'url': page.url,
                    'destination': str(source.resolve()), 'max_bytes': max_bytes,
                })
                segments = result.get('segments')
                if type(segments) is not int or not 0 <= segments <= 20:
                    raise JobError('PROVIDER_PROTOCOL', 'Invalid image segmentation')
                phase = 'decode'
                await decoder.request({
                    'source': str(source.resolve()), 'destination': str(destination.resolve()),
                    'segments': segments, 'max_bytes': max_bytes, 'max_pixels': max_pixels,
                    'budget': budget,
                })
                budget -= destination.stat().st_size
            except BaseException:
                destination.unlink(missing_ok=True)
                # No source URLs, SDK response bodies or credentials in logs.
                logging.getLogger(__name__).info('agent_low_memory_failed phase=%s page=%s', phase, page.number)
                raise
            finally:
                source.unlink(missing_ok=True)
