"""Built-in Agent using the same leases and execution pipeline, without HTTP."""
import asyncio
import hashlib
import hmac
import os
from pathlib import Path
import secrets
import shutil
import tempfile

from jmemby.distributed.agent import Agent, Rejected
from jmemby.distributed.config import AgentSettings
from jmemby.distributed.coordinator import LeaseConflict
from jmemby.distributed.nodes import LOCAL_NODE_ID
from jmemby.domain.errors import JobError
from jmemby.storage.cbz import validate_image


class LocalAgent(Agent):
    def __init__(self, settings, coordinator, provider=None):
        # Network credentials are deliberately absent: external clients cannot
        # authenticate as this reserved node. Only this in-process transport runs it.
        config = AgentSettings('', LOCAL_NODE_ID, '',
                               work_dir=str(Path(settings.storage_root) / 'local-agent'),
                               operation_timeout=settings.operation_timeout,
                               max_page_bytes=settings.max_page_bytes, max_pixels=settings.max_pixels)
        super().__init__(config, provider)
        self.coordinator = coordinator
        self.instance = secrets.token_hex(16)

    async def run(self):
        for path in self.root.glob('chapter-*'):
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
        try:
            await self.poll(None)
        finally:
            self.coordinator.invalidate(LOCAL_NODE_ID)

    async def request(self, session, method, path, **kwargs):
        coordinator = self.coordinator
        if path == 'claim':
            # Stay quiet while disabled and resume without restarting the master.
            while not coordinator.enabled(LOCAL_NODE_ID):
                await asyncio.sleep(1)
        try:
            if not coordinator.enabled(LOCAL_NODE_ID):
                raise LeaseConflict('Local node disabled')
            coordinator.connect(LOCAL_NODE_ID, self.instance)
            if path == 'claim' and method == 'POST':
                return {'job': coordinator.claim(LOCAL_NODE_ID), 'probe': coordinator.probe_offer(LOCAL_NODE_ID)}
            if path == 'probe' and method == 'POST':
                coordinator.probe_ack(LOCAL_NODE_ID, kwargs['json']['nonce'])
                return {'ok': True}
            parts = path.split('/')
            if len(parts) < 3 or parts[0] != 'jobs':
                raise ValueError('Unknown local Agent operation')
            identity = (parts[1], LOCAL_NODE_ID, kwargs['headers']['X-Lease'])
            if parts[2] == 'heartbeat' and method == 'POST':
                coordinator.heartbeat(*identity)
                return {'ok': True, 'probe': coordinator.probe_offer(LOCAL_NODE_ID)}
            if parts[2] == 'complete' and method == 'POST':
                coordinator.finish(*identity)
            elif parts[2] == 'fail' and method == 'POST':
                coordinator.fail(*identity, code=kwargs.get('json', {}).get('code'))
            elif parts[2] == 'pages' and len(parts) == 4 and method == 'PUT':
                await self.accept_page(identity, int(parts[3]), kwargs['data'],
                                       kwargs['headers']['X-Content-SHA256'])
            else:
                raise ValueError('Unknown local Agent operation')
            return {'ok': True}
        except LeaseConflict as exc:
            raise Rejected('Local lease expired, cancelled or reassigned', 409) from exc

    async def accept_page(self, identity, number, stream, expected_hash):
        job = self.coordinator.owned(*identity)
        if number not in job.pages:
            raise LeaseConflict('Page does not belong to this chapter')
        # Copy to the same filesystem as the final staging directory. This keeps
        # publication atomic, including when the Agent's work path is on another mount.
        descriptor, name = tempfile.mkstemp(prefix='.upload-local-', suffix='.part', dir=job.stage)
        temporary = Path(name)
        try:
            digest, size = hashlib.sha256(), 0
            with os.fdopen(descriptor, 'wb') as output:
                while chunk := stream.read(65536):
                    self.coordinator.owned(*identity)
                    size += len(chunk)
                    if size > job.max_bytes:
                        raise JobError('SIZE_LIMIT', '主控节点图片超过大小限制')
                    output.write(chunk)
                    digest.update(chunk)
                    await asyncio.sleep(0)
            if not hmac.compare_digest(digest.hexdigest(), expected_hash):
                raise JobError('BAD_IMAGE', '主控节点图片校验失败')
            validation = asyncio.create_task(asyncio.to_thread(validate_image, temporary, job.max_bytes, job.max_pixels))
            try:
                extension = await asyncio.shield(validation)
            except asyncio.CancelledError:
                # Do not unlink a file while the validation thread is reading it.
                await asyncio.gather(validation, return_exceptions=True)
                raise
            self.coordinator.accept(*identity, number, temporary, extension)
        finally:
            temporary.unlink(missing_ok=True)
