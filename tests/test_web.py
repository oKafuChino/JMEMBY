import importlib.util
import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from jmemby.db.store import Store
from jmemby.services.users import UserService
from jmemby.services.tasks import TaskService
from jmemby.web.auth import AdminAuth


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'db')
        self.users = UserService(self.store, {1})
        self.users.register(2)
        self.auth = AdminAuth(self.users)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_only_admin_can_issue_code(self):
        self.users.grant(1, 2, True)
        for user in (2, 3):
            with self.assertRaises(ValueError):
                self.auth.issue(user)

    def test_single_use_rotation_and_logout(self):
        old = self.auth.issue(1)
        code = self.auth.issue(1)
        with self.assertRaises(ValueError):
            self.auth.login(old)
        token = self.auth.login(code)
        self.assertEqual(self.auth.authenticate(token)['user_id'], 1)
        with self.assertRaises(ValueError):
            self.auth.login(code)
        self.auth.logout(token)
        with self.assertRaises(ValueError):
            self.auth.authenticate(token)

    def test_expiration_and_role_revocation(self):
        with patch('jmemby.web.auth.time.monotonic', return_value=0):
            code = self.auth.issue(1)
        with patch('jmemby.web.auth.time.monotonic', return_value=301):
            with self.assertRaises(ValueError):
                self.auth.login(code)
        token = self.auth.login(self.auth.issue(1))
        self.users.admin_ids = frozenset()
        with self.assertRaises(ValueError):
            self.auth.authenticate(token)

    def test_session_expiration_and_login_rate_limit(self):
        with patch('jmemby.web.auth.time.monotonic', return_value=0):
            token = self.auth.login(self.auth.issue(1))
        with patch('jmemby.web.auth.time.monotonic', return_value=28801):
            with self.assertRaises(ValueError):
                self.auth.authenticate(token)
        for _ in range(30):
            with self.assertRaises(ValueError):
                self.auth.login('wrong')
        with self.assertRaisesRegex(ValueError, '过多'):
            self.auth.login(self.auth.issue(1))


HAS_AIOHTTP = importlib.util.find_spec('aiohttp') is not None
if HAS_AIOHTTP:
    from aiohttp.test_utils import TestClient, TestServer
    from jmemby.web.server import create_app


@unittest.skipUnless(HAS_AIOHTTP, 'aiohttp required for HTTP integration tests')
class WebTests(unittest.IsolatedAsyncioTestCase):
    async def test_library_admin_csrf_confirmation_and_removal(self):
        from jmemby.services.library import LibraryService
        from jmemby.domain.models import Chapter, Manga
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory)/'db')
            users=UserService(store,{1});users.register(2)
            auth=AdminAuth(users);service=TaskService(store,None,users=users)
            komga=SimpleNamespace(scan=AsyncMock())
            service.library=LibraryService(store,users,directory,komga)
            manga=Manga('jmcomic','42','Library Test',(),(Chapter('52',1,'Chapter',()),))
            task=store.submit(manga,2,2)
            record=store.chapters(task['id'])[0]
            path=Path(directory)/'library'/record['path']
            path.parent.mkdir(parents=True);path.write_bytes(b'fixture')
            settings=SimpleNamespace(web_public_url='http://localhost:8080')
            client=TestClient(TestServer(create_app(service,auth,settings)))
            await client.start_server()
            data={'manga_id':task['manga_id'],'confirm':True}
            try:
                self.assertEqual((await client.get('/api/library')).status,401)
                self.assertEqual((await client.post('/api/library/remove',json=data,
                    headers={'Origin':settings.web_public_url})).status,401)
                token=auth.login(auth.issue(1));headers={'Cookie':'session='+token,'Origin':settings.web_public_url}
                self.assertEqual((await client.post('/api/library/remove',json=data,headers=headers)).status,403)
                headers['X-CSRF-Token']=auth.authenticate(token)['csrf']
                response=await client.get('/api/library',params={'q':'Library'},headers=headers)
                self.assertEqual((await response.json())['total'],1)
                self.assertEqual((await client.get('/api/library?q=absent',headers=headers)).status,200)
                self.assertEqual((await client.get('/api/library?page=0',headers=headers)).status,400)
                self.assertEqual((await client.post('/api/library/remove',json=data,headers=headers)).status,400)
                self.assertTrue(path.exists())
                store.finish(task['id'],'COMPLETED')
                for invalid in ({'manga_id':task['manga_id']}, {'manga_id':True,'confirm':True}):
                    self.assertEqual((await client.post('/api/library/remove',json=invalid,headers=headers)).status,400)
                headers['Origin']='https://evil.example'
                self.assertEqual((await client.post('/api/library/remove',json=data,headers=headers)).status,403)
                self.assertTrue(path.exists())
                headers['Origin']=settings.web_public_url
                response=await client.post('/api/library/remove',json=data,headers=headers)
                self.assertEqual(response.status,200)
                self.assertTrue((await response.json())['ok'])
                self.assertFalse(path.exists());komga.scan.assert_awaited_once()
                response=await client.get('/api/library',headers=headers)
                self.assertEqual((await response.json())['total'],0)
                users.admin_ids=frozenset()
                self.assertEqual((await client.get('/api/library',headers=headers)).status,401)
            finally:
                await client.close();store.close()

    async def test_user_search_and_current_task_node_assignment(self):
        from jmemby.distributed.config import MasterSettings
        from jmemby.distributed.coordinator import Coordinator
        from jmemby.distributed.nodes import NodeRegistry
        from jmemby.domain.models import Chapter, Manga
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory)/'db')
            users=UserService(store,{1})
            users.register(2,'reader_one','漫画读者')
            users.register(3,'other','Other')
            service=TaskService(store,None,users=users)
            auth=AdminAuth(users)
            coordinator=service.download_coordinator=Coordinator(MasterSettings(True,{}))
            registry=coordinator.registry=NodeRegistry(store)
            registry.create('vps-1','First node',1)
            registry.create('vps-2','Second node',1)
            manga=Manga('jmcomic','42','Book',(),(Chapter('52',1,'Chapter',()),))
            task=store.submit(manga,2,2)
            settings=SimpleNamespace(web_public_url='http://localhost:8080')
            client=TestClient(TestServer(create_app(service,auth,settings)))
            await client.start_server()
            try:
                for path in ('/api/users?q=reader','/api/tasks'):
                    self.assertEqual((await client.get(path)).status,401)
                token=auth.login(auth.issue(1));headers={'Cookie':'session='+token}
                for query in ('2','@reader_one','读者'):
                    response=await client.get('/api/users',params={'q':query},headers=headers)
                    self.assertEqual(response.status,200)
                    self.assertEqual([u['user_id'] for u in await response.json()],[2])
                for query in ('missing','%'):
                    response=await client.get('/api/users',params={'q':query},headers=headers)
                    self.assertEqual(await response.json(),[])
                self.assertEqual((await client.get('/api/users',params={'q':'x'*201},headers=headers)).status,400)
                self.assertEqual((await client.get('/api/users?page=0&q=reader',headers=headers)).status,400)
                async def current_node():
                    response=await client.get('/api/tasks?queue=normal',headers=headers)
                    self.assertEqual(response.status,200)
                    rows=await response.json()
                    return next(t['node'] for t in rows if t['id']==task['id'])
                self.assertIsNone(await current_node())
                store.update(task['id'],status='DOWNLOADING')
                coordinator.assign('vps-1',task,manga)
                self.assertEqual(await current_node(),{'id':'vps-1','label':'First node'})
                registry.update('vps-1','Renamed',1)
                self.assertEqual((await current_node())['label'],'Renamed')
                for status in ('PACKAGING','IMPORT_PENDING','CANCEL_REQUESTED'):
                    store.update(task['id'],status=status)
                    self.assertEqual((await current_node())['id'],'vps-1')
                coordinator.unassign('vps-1')
                store.update(task['id'],status='RETRY_WAIT')
                self.assertIsNone(await current_node())
                coordinator.assign('vps-2',task,manga)
                self.assertEqual((await current_node())['id'],'vps-2')
                coordinator.unassign('vps-2')
                store.update(task['id'],status='COMPLETED')
                self.assertIsNone(await current_node())
                service.download_coordinator=None
                self.assertIsNone(await current_node())
                users.admin_ids=frozenset()
                self.assertEqual((await client.get('/api/users?q=reader',headers=headers)).status,401)
            finally:
                await client.close()
                store.close()

    async def test_batch_submission_auth_csrf_actor_and_progress(self):
        from jmemby.domain.models import Chapter, Manga
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'db')
            users = UserService(store, {1})
            auth = AdminAuth(users)
            provider = SimpleNamespace(source='jmcomic', get_manga=AsyncMock(return_value=
                Manga('jmcomic', '42', 'Batch', (), (Chapter('52', 1, 'Chapter', ()),))))
            service = TaskService(store, provider, users=users)
            settings = SimpleNamespace(web_public_url='http://localhost:8080')
            client = TestClient(TestServer(create_app(service, auth, settings)))
            await client.start_server()
            try:
                self.assertEqual((await client.get('/api/batches')).status, 401)
                self.assertEqual((await client.post('/api/batches', json={'text':'42'},
                    headers={'Origin':settings.web_public_url})).status, 401)
                token = auth.login(auth.issue(1))
                headers = {'Cookie':'session='+token, 'Origin':settings.web_public_url}
                self.assertEqual((await client.post('/api/batches', json={'text':'42'}, headers=headers)).status, 403)
                headers['X-CSRF-Token'] = auth.authenticate(token)['csrf']
                self.assertEqual((await client.post('/api/batches', json={'text':['42']}, headers=headers)).status, 400)
                response = await client.post('/api/batches', json={'text':'42 42 bad','user_id':99,'chat_id':-100,'priority':1}, headers=headers)
                self.assertEqual(response.status, 202)
                batch_id = (await response.json())['id']
                for _ in range(20):
                    response = await client.get('/api/batches/'+batch_id, headers=headers)
                    result = await response.json()
                    if result['status'] == 'completed':
                        break
                    await asyncio.sleep(0)
                self.assertEqual(result['counts']['queued'], 1)
                task = store.get(result['items'][0]['task_id'])
                self.assertEqual((task['user_id'], task['chat_id'], task['priority']), (1, 1, 0))
                response = await client.get('/api/batches', headers=headers)
                self.assertEqual((await response.json())['batch']['id'], batch_id)
                headers['Origin'] = 'https://evil.example'
                self.assertEqual((await client.post('/api/batches', json={'text':'43'}, headers=headers)).status, 403)
            finally:
                await client.close()
                store.close()

    async def test_auth_csrf_and_admin_operations(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'db')
            try:
                users = UserService(store, {1})
                users.register(2)
                auth = AdminAuth(users)
                settings = SimpleNamespace(web_public_url='http://localhost:8080')
                client = TestClient(TestServer(create_app(TaskService(store, None, users=users), auth, settings)))
                await client.start_server()
                try:
                    self.assertEqual((await client.get('/api/users')).status, 401)
                    self.assertEqual((await client.get('/api/history')).status, 401)
                    response = await client.post('/api/login', json={'code': auth.issue(1)}, headers={'Origin': settings.web_public_url})
                    self.assertEqual(response.status, 200)
                    token = response.cookies['session'].value
                    headers = {'Cookie': 'session=' + token, 'Origin': settings.web_public_url}
                    response = await client.get('/api/session', headers=headers)
                    csrf = (await response.json())['csrf']
                    from jmemby.domain.models import Chapter, Manga
                    task = store.submit(Manga('jmcomic', '42', 'Searchable Book', (),
                                              (Chapter('52', 1, 'Chapter', ()),)), 2, 2)
                    response = await client.get('/api/history', headers=headers)
                    history = await response.json()
                    self.assertEqual((history['limit'], history['tasks'][0]['id']), (100, task['id']))
                    response = await client.get('/api/history', params={'q': 'searchable'}, headers=headers)
                    self.assertEqual(len((await response.json())['tasks']), 1)
                    response = await client.get('/api/history', params={'q': 'absent'}, headers=headers)
                    self.assertEqual((await response.json())['tasks'], [])
                    self.assertEqual((await client.get('/api/history', params={'q': 'x'*201}, headers=headers)).status, 400)
                    self.assertEqual((await client.post('/api/action', json={'action':'ban','user_id':2}, headers=headers)).status, 403)
                    headers['X-CSRF-Token'] = csrf
                    self.assertEqual((await client.post('/api/action', json={'action':'ban','user_id':2}, headers=headers)).status, 200)
                    self.assertTrue(users.get(2)['blocked'])
                    headers['Origin'] = 'https://evil.example'
                    self.assertEqual((await client.post('/api/action', json={'action':'unban','user_id':2}, headers=headers)).status, 403)
                    headers['Origin'] = settings.web_public_url
                    self.assertEqual((await client.post('/api/logout', json={}, headers=headers)).status, 200)
                    self.assertEqual((await client.get('/api/users', headers=headers)).status, 401)
                finally:
                    await client.close()
            finally:
                store.close()
