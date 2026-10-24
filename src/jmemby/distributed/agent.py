"""Agent entrypoint: no Telegram token, Komga credentials or shared filesystem."""
import asyncio
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import random
import re
import secrets
import signal
import shutil
import sys
import tempfile

from jmemby.distributed.config import AgentSettings
from jmemby.domain.errors import JobError
from jmemby.domain.models import Page
from jmemby.providers.builtin import JmProvider
from jmemby.runtime import JsonFormatter, instance_lock
from jmemby.storage.cbz import staged_pages, validate_image

logger = logging.getLogger(__name__)


class Rejected(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class Agent:
    def __init__(self, settings, provider=None):
        self.settings = settings
        self.provider = provider or JmProvider(
            json.dumps([sys.executable, '-m', 'jmemby.providers.jm_adapter']), settings.operation_timeout)
        self.root = Path(settings.work_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    async def request(self, session, method, path, **kwargs):
        # Never forward the bearer token to redirects. TLS verification stays enabled.
        async with session.request(method, self.settings.master_url + '/v1/agent/' + path,
                                   allow_redirects=False, **kwargs) as response:
            if response.status != 200:
                raise Rejected(f'Controller returned HTTP {response.status}', response.status)
            data = bytearray()
            async for part in response.content.iter_chunked(65536):
                data.extend(part)
                if len(data) > 1024 * 1024:
                    raise Rejected('Controller response too large')
            return json.loads(data)

    def validate_job(self, job):
        if (not isinstance(job, dict) or not re.fullmatch(r'[0-9a-f]{32}', job.get('id', ''))
                or not re.fullmatch(r'[A-Za-z0-9_-]{32,256}', job.get('lease', ''))
                or type(job.get('lease_seconds')) is not int or not 30 <= job['lease_seconds'] <= 600):
            raise ValueError('Invalid lease')
        entries = job.get('pages')
        if not isinstance(entries, list) or len(entries) > 10000:
            raise ValueError('Invalid page list')
        pages = []
        for item in entries:
            if (not isinstance(item, dict) or type(item.get('number')) is not int
                    or not 1 <= item['number'] <= 1000000 or not isinstance(item.get('url'), str)
                    or not re.fullmatch(r'jm:[0-9]+:[0-9]+', item['url'])):
                raise ValueError('Invalid JM page reference')
            pages.append(Page(item['number'], item['url']))
        if len({p.number for p in pages}) != len(pages):
            raise ValueError('Duplicate page')
        for key in ('max_bytes', 'max_pixels', 'budget'):
            if type(job.get(key)) is not int or job[key] < 0:
                raise ValueError('Invalid limits')
        if not job['max_bytes'] or not job['max_pixels']:
            raise ValueError('Invalid limits')
        return pages

    async def process(self, session, job):
        pages = self.validate_job(job)
        headers = {'X-Lease': job['lease']}
        route = 'jobs/' + job['id']
        max_bytes = min(job['max_bytes'], self.settings.max_page_bytes)
        max_pixels = min(job['max_pixels'], self.settings.max_pixels)
        concurrency = job.get('concurrency')
        if concurrency is None:
            concurrency = self.settings.concurrency
        if type(concurrency) is not int or not 1 <= concurrency <= 16:
            raise ValueError('Invalid download concurrency')

        async def heartbeat():
            while True:
                await self.request(session, 'POST', route + '/heartbeat', headers=headers, json={})
                await asyncio.sleep(min(15, job['lease_seconds'] / 3))

        async def download_and_upload():
            reserve = self.settings.min_free_space_mb * 1024**2
            # At most eight pages per subprocess, all groups belong to this book.
            if shutil.disk_usage(self.root).free < reserve + min(len(pages), 8 * concurrency) * max_bytes:
                raise JobError('DISK_LOW', 'Agent has insufficient temporary disk space')
            with tempfile.TemporaryDirectory(prefix='chapter-', dir=self.root) as directory:
                stage = Path(directory)
                semaphore = asyncio.Semaphore(concurrency)
                received = 0

                async def group_download(group):
                    nonlocal received
                    async with semaphore:
                        await self.provider.download_batch(group, stage, max_bytes, max_pixels,
                                                           min(job['budget'], max_bytes * len(group)))
                        for page in group:
                            files = staged_pages(stage, page.number)
                            if len(files) != 1:
                                raise JobError('BAD_IMAGE', 'Missing page')
                            path = files[0]
                            await asyncio.to_thread(validate_image, path, max_bytes, max_pixels)
                            received += path.stat().st_size
                            if received > job['budget']:
                                raise JobError('SIZE_LIMIT', 'Chapter budget exceeded')
                            with path.open('rb') as stream:
                                checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
                            for attempt in range(3):
                                try:
                                    with path.open('rb') as stream:
                                        await self.request(session, 'PUT', route + '/pages/' + str(page.number),
                                                           headers={**headers, 'X-Content-SHA256': checksum,
                                                                    'Content-Type': 'application/octet-stream'}, data=stream)
                                    break
                                except Rejected:
                                    raise
                                except (OSError, TimeoutError):
                                    if attempt == 2:
                                        raise
                                    await asyncio.sleep(1 + attempt)
                            path.unlink()
                async with asyncio.TaskGroup() as group:
                    for offset in range(0, len(pages), 8):
                        group.create_task(group_download(pages[offset:offset + 8]))
                await self.request(session, 'POST', route + '/complete', headers=headers, json={})

        beat = asyncio.create_task(heartbeat())
        work = asyncio.create_task(download_and_upload())
        try:
            done, _ = await asyncio.wait({beat, work}, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except asyncio.CancelledError:
            raise
        except Exception:
            beat.cancel()
            work.cancel()
            await asyncio.gather(beat, work, return_exceptions=True)
            try:
                await asyncio.wait_for(self.request(session, 'POST', route + '/fail', headers=headers, json={}), 10)
            except Exception:
                pass  # Expired leases are reclaimed by the controller.
            raise
        finally:
            for task in (beat, work):
                task.cancel()
            await asyncio.gather(beat, work, return_exceptions=True)

    async def poll(self, session):
        while True:
            try:
                response = await self.request(session, 'POST', 'claim', json={})
                job = response['job']
                if job:
                    logger.info('agent_chapter_started job=%s', job['id'])
                    await self.process(session, job)
                    logger.info('agent_chapter_finished job=%s', job['id'])
                else:
                    await asyncio.sleep(5 + random.random())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # No remote URLs, credentials or arbitrary server response text in logs.
                logger.warning('agent_download_error type=%s status=%s', type(exc).__name__, getattr(exc, 'status', None))
                await asyncio.sleep(5 + random.random() * 5)

    async def run(self):
        from aiohttp import ClientSession, ClientTimeout

        timeout = ClientTimeout(total=120, connect=10, sock_read=90)
        async with ClientSession(timeout=timeout, trust_env=False, headers={
                'X-Agent-ID': self.settings.agent_id, 'X-Agent-Instance': secrets.token_hex(16),
                'Authorization': 'Bearer ' + self.settings.token}) as session:
            await self.poll(session)


async def run_service(agent):
    """Drain provider subprocesses on SIGTERM, including under OpenRC."""
    loop = asyncio.get_running_loop()
    stopped = asyncio.Event()
    installed = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stopped.set)
            installed.append(sig)
        except (NotImplementedError, RuntimeError):
            pass
    running = asyncio.create_task(agent.run())
    stopping = asyncio.create_task(stopped.wait())
    try:
        done, _ = await asyncio.wait({running, stopping}, return_when=asyncio.FIRST_COMPLETED)
        if running in done:
            await running
    finally:
        for task in (running, stopping):
            task.cancel()
        await asyncio.gather(running, stopping, return_exceptions=True)
        for sig in installed:
            loop.remove_signal_handler(sig)


def log_handler():
    path = os.getenv('AGENT_LOG_FILE')
    if not path:
        return logging.StreamHandler()
    return RotatingFileHandler(path, maxBytes=2*1024*1024, backupCount=2, encoding='utf-8')


def main():
    handler = log_handler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(handlers=[handler], level=logging.INFO)
    try:
        agent = Agent(AgentSettings.from_env())
        with instance_lock(agent.root / 'agent.lock'):
            # Only our own transient chapter directories, under the configured root.
            for path in agent.root.glob('chapter-*'):
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
            asyncio.run(run_service(agent))
    except (ValueError, RuntimeError) as exc:
        raise SystemExit(str(exc)) from None
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
