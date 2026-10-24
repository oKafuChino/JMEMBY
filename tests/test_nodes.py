import asyncio
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from jmemby.db.store import Store
from jmemby.distributed.config import MasterSettings
from jmemby.distributed.coordinator import Coordinator, LeaseConflict
from jmemby.distributed.installer import AGENT_FILES, INSTALL_SCRIPT, agent_bundle
from jmemby.distributed.nodes import NodeRegistry
from jmemby.domain.errors import JobError
from jmemby.domain.models import Page


def ticket_from(result):
    # Parse shell words, never execute the generated installation command.
    return shlex.split(shlex.split(result['command'])[2])[-1]


class NodeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'db'
        self.store = Store(self.path)
        self.now = 1000
        self.registry = NodeRegistry(self.store, clock=lambda: self.now)
        self.registry.set_origin('https://agents.example.com')
        self.registry.create('vps-1', 'First', 2)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_single_use_ticket_and_hash_only_storage(self):
        issued = self.registry.issue('vps-1')
        ticket = ticket_from(issued)
        self.assertEqual(issued['expires_at'], 2800)
        self.assertNotIn(ticket, json.dumps(self.registry.get('vps-1')))
        config = self.registry.enroll('vps-1', ticket)
        self.assertTrue(self.registry.authenticate('vps-1', config['AGENT_TOKEN']))
        self.assertFalse(self.registry.authenticate('vps-1', ticket))
        self.assertNotIn(config['AGENT_TOKEN'], json.dumps(self.registry.get('vps-1')))
        self.assertNotIn('token_hash', self.registry.public('vps-1'))
        self.assertNotIn('ticket_hash', self.registry.public('vps-1'))
        with self.assertRaises(ValueError):
            self.registry.enroll('vps-1', ticket)

    def test_ticket_expiry_boundary_and_wrong_node(self):
        ticket = ticket_from(self.registry.issue('vps-1'))
        self.registry.create('vps-2', 'Second', 1)
        with self.assertRaises(ValueError):
            self.registry.enroll('vps-2', ticket)
        self.now += self.registry.TICKET_TTL
        with self.assertRaises(ValueError):
            self.registry.enroll('vps-1', ticket)

    def test_regenerate_rotates_ticket_then_token_only_on_redemption(self):
        original = self.registry.enroll('vps-1', ticket_from(self.registry.issue('vps-1')))
        first = ticket_from(self.registry.issue('vps-1'))
        second = ticket_from(self.registry.issue('vps-1'))
        with self.assertRaises(ValueError):
            self.registry.enroll('vps-1', first)
        self.assertTrue(self.registry.authenticate('vps-1', original['AGENT_TOKEN']))
        current = self.registry.enroll('vps-1', second)
        self.assertFalse(self.registry.authenticate('vps-1', original['AGENT_TOKEN']))
        self.assertTrue(self.registry.authenticate('vps-1', current['AGENT_TOKEN']))

    def test_disabling_revokes_ticket_and_denies_agent(self):
        token = self.registry.enroll('vps-1', ticket_from(self.registry.issue('vps-1')))['AGENT_TOKEN']
        ticket = ticket_from(self.registry.issue('vps-1'))
        revision = self.registry.get('vps-1')['revision']
        self.registry.set_enabled('vps-1', False)
        self.assertGreater(self.registry.get('vps-1')['revision'], revision)
        self.assertFalse(self.registry.authenticate('vps-1', token))
        with self.assertRaises(ValueError): self.registry.issue('vps-1')
        self.registry.set_enabled('vps-1', True)
        self.assertTrue(self.registry.authenticate('vps-1', token))
        with self.assertRaises(ValueError): self.registry.enroll('vps-1', ticket)

    def test_origin_change_invalidates_unused_tickets(self):
        ticket = ticket_from(self.registry.issue('vps-1'))
        self.registry.set_origin('https://other.example.com:8443/')
        self.assertEqual(self.registry.origin(), 'https://other.example.com:8443')
        with self.assertRaises(ValueError): self.registry.enroll('vps-1', ticket)

    def test_restart_tombstones_and_environment_is_seed_only(self):
        NodeRegistry(self.store, {'legacy': 'a'*43})
        NodeRegistry(self.store, {'legacy': 'b'*43})
        self.assertTrue(self.registry.authenticate('legacy', 'a'*43))
        self.registry.delete('legacy')
        self.store.close()
        self.store = Store(self.path)
        self.registry = NodeRegistry(self.store, {'legacy': 'a'*43})
        self.assertNotIn('legacy', self.registry.ids())
        self.registry.create('legacy', 'Recreated', 4)
        self.assertFalse(self.registry.authenticate('legacy', 'a'*43))

    def test_max_nodes_and_field_validation(self):
        for name in ('../x', 'x/y', '', 'x'*49, '$(whoami)'):
            with self.subTest(name=name), self.assertRaises(ValueError): self.registry.create(name, 'Name', 1)
        for value in (0,17,True,1.5,'2'):
            with self.subTest(value=value), self.assertRaises(ValueError): self.registry.update('vps-1', 'Name', value)
        for label in ('', ' ', 'bad\nname', 'x'*65):
            with self.assertRaises(ValueError): self.registry.update('vps-1', label, 1)
        self.registry.update('vps-1', ' New ', 16)
        self.assertEqual(self.registry.get('vps-1')['label'], 'New')
        for number in range(31): self.registry.create(str(number), 'Node', 1)
        with self.assertRaises(ValueError): self.registry.create('overflow', 'Node', 1)
        self.registry.delete('0')
        self.registry.create('overflow', 'Node', 1)

    def test_public_url_and_empty_seed_configuration(self):
        for origin in ('http://public.example', 'https://u:p@host', 'https://host/path',
                       'https://host?q=x', 'https://host#x', 'https://host:0',
                       'https://host:65536', 'https://host:bad', 'https://$(whoami)',
                       'https://a..b', 'https://-bad', 'https://a\nb', 'https://host;id'):
            with self.subTest(origin=origin), self.assertRaises(ValueError): self.registry.set_origin(origin)
        self.registry.set_origin('https://[::1]:8443')
        with patch.dict(os.environ, {'DISTRIBUTED_ENABLED':'true'}, clear=True):
            self.assertEqual(MasterSettings.from_env().tokens, {})

    def test_schema_eight_migration_preserves_settings_and_is_idempotent(self):
        self.store.connection.executescript('DROP TABLE download_nodes; PRAGMA user_version=8;')
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.connection.execute('PRAGMA user_version').fetchone()[0],9)
        self.assertEqual(NodeRegistry(self.store).origin(),'https://agents.example.com')
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(NodeRegistry(self.store).ids(), [])


class InstallerTests(unittest.TestCase):
    def test_bundle_allowlist_and_checksum(self):
        payload, checksum = agent_bundle()
        self.assertEqual(hashlib.sha256(payload).hexdigest(),checksum)
        with tarfile.open(fileobj=io.BytesIO(payload), mode='r:gz') as archive:
            self.assertEqual(set(archive.getnames()), {'Dockerfile'} | {'src/jmemby/'+f for f in AGENT_FILES})
            for entry in archive:
                self.assertTrue(entry.isfile())
                self.assertEqual(entry.mtime,0)
            dockerfile=archive.extractfile('Dockerfile').read().decode()
            self.assertIn('COPY --chown=agent:agent',dockerfile)

    def test_installer_extracts_versioned_source_and_four_line_environment(self):
        # Exercise the embedded Python with a local fake enrollment, never install Docker.
        script = INSTALL_SCRIPT.split("<<'PY'\n",1)[1].split('\nPY\n',1)[0]
        compile(script, '<installer-python>', 'exec')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); temporary=root/'tmp'; temporary.mkdir(); target=root/'install';target.mkdir()
            payload, checksum=agent_bundle()
            (temporary/'agent.tar.gz').write_bytes(payload)
            config={'AGENT_ID':'vps-1','AGENT_MASTER_URL':'https://agents.example.com','AGENT_TOKEN':'a'*43,'AGENT_CONCURRENCY':2}
            (temporary/'enrollment.json').write_text(json.dumps({'config':config,'bundle_sha256':checksum}))
            run=subprocess.run([sys.executable,'-c',script,str(temporary),str(target),'vps-1'],capture_output=True,text=True)
            self.assertEqual(run.returncode,0,run.stderr)
            env=(target/'.env.agent').read_text().splitlines()
            self.assertEqual(len(env),4)
            self.assertEqual(dict(x.split('=',1) for x in env),{k:str(v) for k,v in config.items()})
            compose=json.loads((target/'compose.json').read_text())
            self.assertEqual(compose['services']['agent']['build']['context'],'./releases/'+checksum)
            self.assertNotIn('ports',compose['services']['agent'])
            self.assertTrue((target/'releases'/checksum/'src/jmemby/distributed/agent.py').is_file())
            (temporary/'agent.tar.gz').write_bytes(b'bad')
            run=subprocess.run([sys.executable,'-c',script,str(temporary),str(target),'vps-1'],capture_output=True)
            self.assertNotEqual(run.returncode,0)
            self.assertEqual((target/'.env.agent').read_text().splitlines(),env)


class NodeCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_configuration_change_revokes_lease_and_reports_new_concurrency(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory)/'db')
            try:
                coordinator=Coordinator(MasterSettings(True,{}))
                coordinator.registry=NodeRegistry(store)
                coordinator.registry.create('vps-1','Node',4)
                coordinator.touch('vps-1')
                coordinator.books['vps-1']={'source_id':'1','title':'Book','task_id':'task'}
                work=asyncio.create_task(coordinator.download_batch('vps-1',[Page(1,'jm:1:0')],Path(directory),1000,1000,1000))
                await asyncio.sleep(0)
                job=coordinator.claim('vps-1')
                self.assertEqual(job['concurrency'],4)
                coordinator.registry.set_enabled('vps-1',False)
                coordinator.invalidate('vps-1')
                self.assertFalse(coordinator.online('vps-1'))
                with self.assertRaises(LeaseConflict): coordinator.owned(job['id'],'vps-1',job['lease'])
                with self.assertRaises(JobError) as error: await asyncio.wait_for(work,2)
                self.assertEqual(error.exception.code,'AGENT_CHANGED')
            finally: store.close()


HAS_AIOHTTP=importlib.util.find_spec('aiohttp') is not None
if HAS_AIOHTTP:
    from aiohttp.test_utils import TestClient, TestServer


@unittest.skipUnless(HAS_AIOHTTP,'aiohttp required for HTTP integration tests')
class NodeHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from jmemby.distributed.server import create_app
        self.temp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.temp.name)/'db')
        self.coordinator=Coordinator(MasterSettings(True,{}))
        self.registry=self.coordinator.registry=NodeRegistry(self.store)
        self.registry.set_origin('https://agents.example.com')
        self.registry.create('vps-1','Node',2)
        self.client=TestClient(TestServer(create_app(self.coordinator)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.store.close()
        self.temp.cleanup()

    async def test_bootstrap_public_but_enrollment_and_agent_auth_required(self):
        for path in ('/install/agent.sh','/install/agent.tar.gz'):
            response=await self.client.get(path)
            self.assertEqual(response.status,200)
            self.assertEqual(response.headers['Cache-Control'],'no-store')
        ticket=ticket_from(self.registry.issue('vps-1'))
        self.assertEqual((await self.client.post('/v1/agent/claim',json={})).status,401)
        self.assertEqual((await self.client.post('/install/enroll/vps-1')).status,401)
        headers={'Authorization':'Install '+ticket}
        response=await self.client.post('/install/enroll/vps-1',headers=headers)
        self.assertEqual(response.status,200)
        data=await response.json()
        self.assertEqual((await self.client.post('/install/enroll/vps-1',headers=headers)).status,401)
        headers={'Authorization':'Bearer '+data['config']['AGENT_TOKEN'],'X-Agent-ID':'vps-1','X-Agent-Instance':'a'*32}
        self.assertEqual((await self.client.post('/v1/agent/claim',json={},headers=headers)).status,200)
        self.registry.set_enabled('vps-1',False)
        self.assertEqual((await self.client.post('/v1/agent/claim',json={},headers=headers)).status,401)

    async def test_admin_nodes_auth_csrf_crud_and_active_deletion(self):
        from jmemby.services.users import UserService
        from jmemby.services.tasks import TaskService
        from jmemby.web.auth import AdminAuth
        from jmemby.web.server import create_app
        users=UserService(self.store,{1});auth=AdminAuth(users)
        service=TaskService(self.store,None,users=users)
        service.download_coordinator=self.coordinator
        settings=SimpleNamespace(web_public_url='http://localhost:8080')
        client=TestClient(TestServer(create_app(service,auth,settings)))
        await client.start_server()
        try:
            self.assertEqual((await client.get('/api/nodes')).status,401)
            token=auth.login(auth.issue(1))
            headers={'Cookie':'session='+token,'Origin':settings.web_public_url}
            create={'action':'create','id':'vps-2','label':'New','concurrency':3}
            self.assertEqual((await client.post('/api/nodes',json=create,headers=headers)).status,403)
            headers['X-CSRF-Token']=auth.authenticate(token)['csrf']
            self.assertEqual((await client.post('/api/nodes',json=create,headers=headers)).status,201)
            response=await client.get('/api/nodes',headers=headers)
            data=await response.json()
            self.assertEqual(len(data['agents']),2)
            self.assertNotIn('ticket_hash',json.dumps(data))
            self.coordinator.books['vps-2']={}
            self.assertEqual((await client.post('/api/nodes',json={'action':'delete','id':'vps-2'},headers=headers)).status,400)
            self.coordinator.books.pop('vps-2')
            self.assertEqual((await client.post('/api/nodes',json={'action':'delete','id':'vps-2'},headers=headers)).status,200)
            headers['Origin']='https://evil.example'
            self.assertEqual((await client.post('/api/nodes',json=create,headers=headers)).status,403)
            users.admin_ids=frozenset()
            self.assertEqual((await client.get('/api/nodes',headers=headers)).status,401)
        finally: await client.close()
