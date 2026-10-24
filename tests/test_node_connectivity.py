import asyncio
import importlib.util
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from jmemby.db.store import Store
from jmemby.distributed.agent import Agent, Rejected
from jmemby.distributed.config import AgentSettings, MasterSettings
from jmemby.distributed.coordinator import Coordinator, LeaseConflict
from jmemby.distributed.nodes import NodeRegistry
from jmemby.domain.models import Page


TOKENS = {'one':'a'*43, 'two':'b'*43}


class ConnectivityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name)/'db')
        self.coordinator = Coordinator(MasterSettings(True, TOKENS))
        self.coordinator.registry = NodeRegistry(self.store, TOKENS)
        self.coordinator.connect('one', 'a'*32)
        self.coordinator.connect('two', 'b'*32)
        self.operations = []

    async def asyncTearDown(self):
        for operation in self.operations:
            operation.cancel()
        await asyncio.gather(*self.operations, return_exceptions=True)
        self.store.close()
        self.temp.cleanup()

    async def start(self, name='one'):
        operation = asyncio.create_task(self.coordinator.test_connection(name))
        self.operations.append(operation)
        await asyncio.sleep(0)
        return operation

    async def test_challenge_confirmation_reports_success_without_tasks_or_secret_leak(self):
        work = await self.start()
        nonce = self.coordinator.probe_offer('one')
        self.assertTrue(nonce)
        self.assertNotIn(nonce, str(self.coordinator.snapshot()))
        self.assertFalse(work.done())
        self.coordinator.probe_ack('one', nonce)
        result = await work
        self.assertEqual(result['status'], 'success')
        self.assertGreaterEqual(result['elapsed_ms'], 0)
        self.assertTrue(self.coordinator.online('one'))
        self.assertFalse(self.coordinator.probes)
        self.assertFalse(self.coordinator.books)
        self.assertFalse(self.coordinator.jobs)
        self.assertEqual(self.store.connection.execute('SELECT COUNT(*) FROM tasks').fetchone()[0],0)
        self.assertEqual(self.coordinator.snapshot()['agents'][0]['connectivity_test'], result)
        with self.assertRaises(LeaseConflict): self.coordinator.probe_ack('one', nonce)

    async def test_recent_heartbeat_is_not_a_successful_probe(self):
        self.coordinator.PROBE_TIMEOUT = 0.01
        self.coordinator.touch('one')
        result = await self.coordinator.test_connection('one')
        self.assertEqual(result['status'], 'timeout')
        self.assertTrue(self.coordinator.online('one'))
        self.assertFalse(self.coordinator.probes)

    async def test_ack_requires_offered_nonce_correct_node_and_same_instance(self):
        work = await self.start()
        nonce = self.coordinator.probes['one']['nonce']
        with self.assertRaises(LeaseConflict): self.coordinator.probe_ack('one', nonce)
        self.assertEqual(self.coordinator.probe_offer('one'), nonce)
        for node, value in (('two', nonce), ('one','wrong'), ('one',None)):
            with self.assertRaises(LeaseConflict): self.coordinator.probe_ack(node,value)
        self.coordinator.instances['one'] = 'c'*32
        with self.assertRaises(LeaseConflict): self.coordinator.probe_ack('one',nonce)
        self.coordinator.instances['one'] = 'a'*32
        self.coordinator.probe_ack('one',nonce)
        self.assertEqual((await work)['status'],'success')

    async def test_expired_challenge_cannot_succeed(self):
        work = await self.start()
        nonce = self.coordinator.probe_offer('one')
        self.coordinator.probes['one']['started'] -= 30
        self.assertIsNone(self.coordinator.probe_offer('one'))
        with self.assertRaises(LeaseConflict): self.coordinator.probe_ack('one',nonce)
        work.cancel()
        await asyncio.gather(work,return_exceptions=True)
        self.assertFalse(self.coordinator.probes)

    async def test_duplicate_probe_is_rejected_and_nodes_are_independent(self):
        one = await self.start()
        with self.assertRaisesRegex(ValueError,'正在测试'):
            await self.coordinator.test_connection('one')
        two = await self.start('two')
        for name in ('one','two'):
            self.coordinator.probe_ack(name,self.coordinator.probe_offer(name))
        self.assertEqual([r['status'] for r in await asyncio.gather(one,two)],['success','success'])

    async def test_invalidation_interrupts_probe_and_removes_previous_result(self):
        self.coordinator.probe_results['one']={'status':'success'}
        work = await self.start()
        self.coordinator.registry.set_enabled('one',False)
        self.coordinator.invalidate('one')
        self.assertEqual((await work)['status'],'changed')
        self.assertNotIn('one',self.coordinator.probe_results)
        self.assertFalse(self.coordinator.probes)

    async def test_missing_disabled_and_unregistered_nodes_rejected(self):
        with self.assertRaisesRegex(ValueError,'不存在'): await self.coordinator.test_connection('missing')
        self.coordinator.registry.set_enabled('one',False)
        with self.assertRaisesRegex(ValueError,'启用'): await self.coordinator.test_connection('one')
        self.coordinator.registry.create('new','New',1)
        with self.assertRaisesRegex(ValueError,'尚未安装'): await self.coordinator.test_connection('new')

    async def test_cancelled_request_drops_pending_challenge(self):
        work = await self.start()
        nonce = self.coordinator.probe_offer('one')
        work.cancel()
        await asyncio.gather(work,return_exceptions=True)
        self.assertFalse(self.coordinator.probes)
        with self.assertRaises(LeaseConflict): self.coordinator.probe_ack('one',nonce)

    async def test_agent_ack_and_failure_isolation(self):
        agent = Agent(AgentSettings('https://example.com','one',TOKENS['one'],work_dir=self.temp.name))
        work = await self.start()
        nonce = self.coordinator.probe_offer('one')
        async def request(session, method, path, **kwargs):
            self.assertEqual((method,path),('POST','probe'))
            self.coordinator.probe_ack('one',kwargs['json']['nonce'])
        agent.request = AsyncMock(side_effect=request)
        await agent.answer_probe(None,{'probe':nonce})
        self.assertEqual((await work)['status'],'success')
        agent.request.reset_mock()
        for value in ({},None,{'probe':'bad'}, {'probe':{}}): await agent.answer_probe(None,value)
        agent.request.assert_not_called()
        agent.request.side_effect = Rejected('expired',409)
        await agent.answer_probe(None,{'probe':nonce})  # A diagnostic failure must not cancel a download.

    async def test_busy_download_answers_probe_on_heartbeat_and_completes(self):
        from PIL import Image
        root=Path(self.temp.name)
        self.coordinator.touch('one')
        self.coordinator.books['one']={'task_id':'task','source_id':'1','title':'Book'}
        download=asyncio.create_task(self.coordinator.download_batch(
            'one',[Page(1,'jm:1:0')],root,10000,10000,10000))
        self.operations.append(download)
        await asyncio.sleep(0)
        job=self.coordinator.claim('one')
        probe=await self.start()
        async def write_pages(pages,stage,*limits):
            await asyncio.sleep(0.01)
            for page in pages:Image.new('RGB',(2,2),'white').save(stage/f'{page.number:06d}.png')
        agent=Agent(AgentSettings('https://example.com','one',TOKENS['one'],
                    work_dir=str(root/'agent'),min_free_space_mb=0,max_page_bytes=10000),
                    SimpleNamespace(download_batch=write_pages))
        async def request(session,method,path,**kwargs):
            if path=='probe':
                self.coordinator.probe_ack('one',kwargs['json']['nonce'])
                return {'ok':True}
            identity=(job['id'],'one',job['lease'])
            if path.endswith('/heartbeat'):
                self.coordinator.heartbeat(*identity)
                return {'ok':True,'probe':self.coordinator.probe_offer('one')}
            if '/pages/' in path:
                temporary=root/'upload.png';temporary.write_bytes(kwargs['data'].read())
                self.coordinator.accept(*identity,1,temporary,'.png')
            elif path.endswith('/complete'):
                self.coordinator.finish(*identity)
            else:self.fail('Unexpected transport route: '+path)
            return {'ok':True}
        agent.request=request
        await asyncio.wait_for(agent.process(None,job),2)
        self.assertEqual((await probe)['status'],'success')
        await asyncio.wait_for(download,2)
        self.assertTrue((root/'000001.png').is_file())


HAS_AIOHTTP = importlib.util.find_spec('aiohttp') is not None


@unittest.skipUnless(HAS_AIOHTTP,'aiohttp required for HTTP integration tests')
class ConnectivityHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_to_agent_round_trip_with_auth_and_csrf(self):
        from aiohttp.test_utils import TestClient, TestServer
        from jmemby.distributed.server import create_app as agent_app
        from jmemby.web.server import create_app as admin_app
        from jmemby.services.users import UserService
        from jmemby.services.tasks import TaskService
        from jmemby.web.auth import AdminAuth
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory)/'db')
            coordinator=Coordinator(MasterSettings(True,TOKENS))
            coordinator.registry=NodeRegistry(store,TOKENS)
            users=UserService(store,{1});auth=AdminAuth(users)
            service=TaskService(store,None,users=users);service.download_coordinator=coordinator
            settings=SimpleNamespace(web_public_url='http://localhost:8080')
            admin=TestClient(TestServer(admin_app(service,auth,settings)))
            agent=TestClient(TestServer(agent_app(coordinator)))
            await admin.start_server();await agent.start_server()
            work=None
            try:
                data={'action':'test','id':'one'}
                self.assertEqual((await admin.post('/api/nodes',json=data,headers={'Origin':settings.web_public_url})).status,401)
                token=auth.login(auth.issue(1))
                headers={'Cookie':'session='+token,'Origin':settings.web_public_url}
                self.assertEqual((await admin.post('/api/nodes',json=data,headers=headers)).status,403)
                headers['X-CSRF-Token']=auth.authenticate(token)['csrf']
                work=asyncio.ensure_future(admin.post('/api/nodes',json=data,headers=headers))
                async with asyncio.timeout(2):
                    while not coordinator.probes:await asyncio.sleep(0.01)
                ah={'Authorization':'Bearer '+TOKENS['one'],'X-Agent-ID':'one','X-Agent-Instance':'a'*32}
                response=await agent.post('/v1/agent/claim',json={},headers=ah)
                body=await response.json();self.assertIsNone(body['job'])
                self.assertEqual((await agent.post('/v1/agent/probe',json={'nonce':body['probe']})).status,401)
                self.assertEqual((await agent.post('/v1/agent/probe',json={'nonce':body['probe']},headers=ah)).status,200)
                response=await work
                self.assertEqual(response.status,200)
                self.assertEqual((await response.json())['status'],'success')
                self.assertEqual((await agent.post('/v1/agent/probe',json={'nonce':body['probe']},headers=ah)).status,409)
                headers['Origin']='https://evil.example'
                self.assertEqual((await admin.post('/api/nodes',json=data,headers=headers)).status,403)
            finally:
                if work:
                    work.cancel();await asyncio.gather(work,return_exceptions=True)
                await admin.close();await agent.close();store.close()
