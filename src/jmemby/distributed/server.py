"""Separate token-authenticated transport; never exposes the admin web API."""
import asyncio
import hashlib
import hmac
import logging
import os
import re
import tempfile
from pathlib import Path

from aiohttp import web

from jmemby.distributed.coordinator import LeaseConflict
from jmemby.domain.errors import JobError
from jmemby.storage.cbz import validate_image

logger = logging.getLogger(__name__)


def create_app(coordinator):
    @web.middleware
    async def authenticate(request, handler):
        # Bootstrap routes have their own short-lived enrollment authentication.
        if request.match_info.route.name in {'agent-installer', 'agent-bundle', 'agent-enroll'}:
            response = await handler(request)
            response.headers.update({'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})
            return response
        agent = request.headers.get('X-Agent-ID', '')
        supplied = request.headers.get('Authorization', '')
        if not supplied.startswith('Bearer ') or not coordinator.authenticate(agent, supplied[7:]):
            return web.json_response({'error': 'Unauthorized'}, status=401)
        request['agent'] = agent
        try:
            coordinator.connect(agent, request.headers.get('X-Agent-Instance', ''))
            response = await handler(request)
        except LeaseConflict:
            response = web.json_response({'error': 'Lease expired, cancelled or reassigned'}, status=409)
        except (ValueError, TypeError, KeyError):
            response = web.json_response({'error': 'Invalid request'}, status=400)
        except JobError as exc:
            response = web.json_response({'error': exc.code}, status=422)
        except TimeoutError:
            response = web.json_response({'error': 'Upload timed out'}, status=408)
        except OSError:
            response = web.json_response({'error': 'Storage unavailable'}, status=503)
        response.headers['Cache-Control'] = 'no-store'
        return response

    app = web.Application(middlewares=[authenticate], client_max_size=4096)
    upload_slots = asyncio.Semaphore(4)

    async def installer(request):
        from jmemby.distributed.installer import INSTALL_SCRIPT
        return web.Response(text=INSTALL_SCRIPT, content_type='text/plain')

    async def bundle(request):
        from jmemby.distributed.installer import agent_bundle
        data, _ = agent_bundle()
        return web.Response(body=data, content_type='application/gzip')

    async def enroll(request):
        from jmemby.distributed.installer import agent_bundle
        registry = coordinator.registry
        supplied = request.headers.get('Authorization', '')
        if not registry or not supplied.startswith('Install '):
            return web.json_response({'error': 'Invalid enrollment'}, status=401)
        # Build before consuming the ticket: a packaging failure must not rotate credentials.
        _, checksum = agent_bundle()
        try:
            config = registry.enroll(request.match_info['agent_id'], supplied[8:])
        except ValueError:
            return web.json_response({'error': 'Invalid or expired enrollment'}, status=401)
        coordinator.invalidate(config['AGENT_ID'])
        return web.json_response({'config': config, 'bundle_sha256': checksum})

    def ownership(request):
        return (request.match_info['job_id'], request['agent'], request.headers.get('X-Lease', ''))

    async def claim(request):
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError('Object required')
        return web.json_response({'job': coordinator.claim(request['agent'])})

    async def heartbeat(request):
        coordinator.heartbeat(*ownership(request))
        return web.json_response({'ok': True})

    async def complete(request):
        coordinator.finish(*ownership(request))
        return web.json_response({'ok': True})

    async def fail(request):
        coordinator.fail(*ownership(request))
        return web.json_response({'ok': True})

    async def receive_upload(request):
        identity = ownership(request)
        chunk = coordinator.owned(*identity)
        number = int(request.match_info['number'])
        if number not in chunk.pages:
            raise LeaseConflict()
        expected_hash = request.headers.get('X-Content-SHA256', '')
        if (not re.fullmatch(r'[0-9a-f]{64}', expected_hash)
                or request.headers.get('Content-Encoding', 'identity') != 'identity'):
            raise ValueError('Hash and uncompressed body required')
        if request.content_length is not None and request.content_length > chunk.max_bytes:
            raise web.HTTPRequestEntityTooLarge(max_size=chunk.max_bytes, actual_size=request.content_length)
        key = (chunk.id, number)
        if key in coordinator.uploads:
            raise LeaseConflict('Page upload already active')
        coordinator.uploads.add(key)
        temporary = None
        try:
            fd, name = tempfile.mkstemp(prefix='.upload-', suffix='.part', dir=chunk.stage)
            temporary = Path(name)
            size, digest = 0, hashlib.sha256()
            async with asyncio.timeout(120):
                with os.fdopen(fd, 'wb') as stream:
                    async for data in request.content.iter_chunked(65536):
                        coordinator.owned(*identity)
                        size += len(data)
                        if size > chunk.max_bytes:
                            raise web.HTTPRequestEntityTooLarge(max_size=chunk.max_bytes, actual_size=size)
                        digest.update(data)
                        stream.write(data)
            if not hmac.compare_digest(digest.hexdigest(), expected_hash):
                raise ValueError('Checksum mismatch')
            extension = await asyncio.to_thread(validate_image, temporary, chunk.max_bytes, chunk.max_pixels)
            coordinator.accept(*identity, number, temporary, extension)
            return web.json_response({'ok': True})
        finally:
            coordinator.uploads.discard(key)
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    async def upload(request):
        # Bound image decoding and temporary uploads across all VPS nodes.
        async with asyncio.timeout(150):
            async with upload_slots:
                return await receive_upload(request)

    prefix = '/v1/agent'
    app.router.add_get('/install/agent.sh', installer, name='agent-installer')
    app.router.add_get('/install/agent.tar.gz', bundle, name='agent-bundle')
    app.router.add_post('/install/enroll/{agent_id}', enroll, name='agent-enroll')
    app.router.add_post(prefix + '/claim', claim)
    app.router.add_post(prefix + '/jobs/{job_id}/heartbeat', heartbeat)
    app.router.add_post(prefix + '/jobs/{job_id}/complete', complete)
    app.router.add_post(prefix + '/jobs/{job_id}/fail', fail)
    app.router.add_put(prefix + '/jobs/{job_id}/pages/{number}', upload)
    return app


async def serve(coordinator):
    runner = web.AppRunner(create_app(coordinator), access_log=None, shutdown_timeout=5)
    await runner.setup()
    try:
        await web.TCPSite(runner, coordinator.settings.host, coordinator.settings.port).start()
        logger.info('distributed_listener_started port=%s', coordinator.settings.port)
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
