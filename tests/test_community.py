import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from jmemby.bot.presentation import MENU, profile_text
from jmemby.bot.reports import deliver_reports
from jmemby.db.store import Store
from jmemby.domain.models import Chapter, Manga
from jmemby.services.community import CommunityService
from jmemby.services.library import LibraryService
from jmemby.services.tasks import TaskService
from jmemby.services.users import UserService


class CommunityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.path=self.root/'db';self.store=Store(self.path)
        self.users=UserService(self.store,{1,9});self.users.register(2);self.users.register(3)
        self.community=CommunityService(self.store,self.users)

    def tearDown(self):
        self.store.close();self.temp.cleanup()

    def book(self, number=42, completed=True):
        manga=Manga('jmcomic',str(number),'Book '+str(number),(),(Chapter(str(number),1,'Chapter',()),))
        task=self.store.submit(manga,2,2)
        if completed:
            self.store.update_chapter(self.store.chapters(task['id'])[0]['id'],status='IMPORTED')
            self.store.finish(task['id'],'COMPLETED')
        return manga,task

    def test_like_is_once_per_user_and_persists(self):
        self.book()
        self.assertTrue(self.community.like(2,'42')['added'])
        self.assertFalse(self.community.like(2,'42')['added'])
        self.users.grant(1,3,True)
        self.assertEqual(self.community.like(3,'42')['likes'],2)
        self.store.close();self.store=Store(self.path)
        self.community=CommunityService(self.store,UserService(self.store,{1,9}))
        self.assertEqual(self.community.rank(2)[0]['likes'],2)

    def test_rank_top_ten_and_only_available_manga(self):
        self.assertEqual(self.community.rank(2),[])
        self.assertIsNone(self.community.random(2))
        for n in range(1,13):
            self.book(n);self.community.like(2,str(n))
        self.community.like(3,'12')
        rows=self.community.rank(2)
        self.assertEqual(len(rows),10)
        self.assertEqual([r['source_id'] for r in rows],['12']+[str(n) for n in range(1,10)])
        self.book(99,False)
        for _ in range(20):self.assertNotEqual(self.community.random(2)['source_id'],'99')
        self.assertEqual(self.store.connection.execute('SELECT COUNT(*) FROM tasks').fetchone()[0],13)

    def test_removed_books_disappear_until_successful_redownload(self):
        manga,task=self.book();self.community.like(2,'42')
        LibraryService(self.store,self.users,self.root,None).remove_files(1,task['manga_id'])
        self.assertEqual(self.community.rank(2),[]);self.assertIsNone(self.community.random(2))
        with self.assertRaises(ValueError):self.community.like(3,'42')
        second=self.store.submit(manga,2,2)
        self.assertIsNone(self.community.random(2))
        self.store.update_chapter(self.store.chapters(second['id'])[0]['id'],status='IMPORTED')
        self.store.finish(second['id'],'COMPLETED')
        self.assertEqual(self.community.rank(2)[0]['likes'],1)

    def test_membership_invalid_ids_and_empty_library(self):
        self.book()
        for source_id in ('', '0', '042', '-1', '42 extra','https://example.com/42',"42' OR 1=1",None,'7'*17,'99'):
            with self.assertRaises(ValueError):self.community.like(2,source_id)
        self.users.set_blocked(1,3,True)
        for actor in (3,4):
            for action in (lambda:self.community.like(actor,'42'),lambda:self.community.report(actor,'42'),
                           lambda:self.community.rank(actor),lambda:self.community.random(actor)):
                with self.assertRaises(ValueError):action()

    async def test_report_deduplicates_and_retries_each_admin_after_restart(self):
        self.book()
        self.assertTrue(self.community.report(2,'42')['added'])
        self.assertFalse(self.community.report(2,'42')['added'])
        self.assertEqual(len(self.community.pending_reports()),2)
        async def send(admin,text,**kwargs):
            if admin==9:raise RuntimeError('offline')
        bot=SimpleNamespace(send_message=AsyncMock(side_effect=send))
        await deliver_reports(bot,self.community)
        rows=self.store.connection.execute('SELECT * FROM report_deliveries ORDER BY admin_id').fetchall()
        self.assertEqual([r['status'] for r in rows],['SENT','PENDING'])
        self.assertEqual(rows[1]['attempts'],1)
        self.assertEqual(self.community.pending_reports(),[])
        self.store.close();self.store=Store(self.path)
        self.community=CommunityService(self.store,UserService(self.store,{1,9}))
        self.store.connection.execute('UPDATE report_deliveries SET next_at=0')
        bot.send_message=AsyncMock()
        await deliver_reports(bot,self.community)
        self.assertEqual(bot.send_message.await_count,1)
        self.assertEqual(bot.send_message.call_args.args[0],9)
        self.assertIn('举报用户 ID：2',bot.send_message.call_args.args[1])
        await deliver_reports(bot,self.community)
        self.assertEqual(bot.send_message.await_count,1)

    async def test_report_does_not_notify_revoked_admin(self):
        self.book();self.community.report(2,'42')
        self.users.admin_ids=frozenset({1})
        bot=SimpleNamespace(send_message=AsyncMock())
        await deliver_reports(bot,self.community)
        bot.send_message.assert_awaited_once()
        self.assertEqual(bot.send_message.call_args.args[0],1)
        self.users.admin_ids=frozenset()
        with self.assertRaises(ValueError):self.community.report(3,'42')

    def test_profile_shows_email_and_public_address_without_credentials(self):
        reading={'status':'READY','email':'reader@example.com','password_encrypted':'SECRET','password':'SECRET'}
        text=profile_text(2,'已注册',reading,'https://read.example.com')
        self.assertIn('reader@example.com',text);self.assertIn('https://read.example.com',text)
        self.assertNotIn('SECRET',text)
        self.assertIn('尚未开通',profile_text(2,'已注册',None,''))
        self.assertEqual(MENU['👤 我的账号'],'/my')
        for command in ('/queue','/status','/cancel','/retry','/library'):
            self.assertNotIn(command,MENU.values())

    async def test_direct_jm_no_confirmation_and_rechecks_duplicate_and_membership(self):
        manga=Manga('jmcomic','42','Book',(),(Chapter('52',1,'Chapter',()),))
        provider=SimpleNamespace(source='jmcomic',get_manga=AsyncMock(return_value=manga))
        service=TaskService(self.store,provider,users=self.users)
        results=await asyncio.gather(service.download('42',2,-100),service.download('42',2,-200),return_exceptions=True)
        self.assertEqual(sum(isinstance(r,ValueError) for r in results),1)
        self.assertEqual(len(self.store.tasks()),1)
        self.assertEqual(self.store.connection.execute('SELECT COUNT(*) FROM confirmations').fetchone()[0],0)
        task=self.store.tasks()[0];self.assertEqual(task['chat_id'],-100)
        self.users.grant(1,3,True)
        with self.assertRaisesRegex(ValueError,'优先队列'):
            await service.download('42',3,-200)
        self.assertEqual(self.store.get(task['id'])['user_id'],2)
        self.assertEqual(self.store.get(task['id'])['priority'],1)
        self.store.finish(task['id'],'FAILED')
        async def block(_):
            self.users.set_blocked(1,2,True);return manga
        provider.get_manga=AsyncMock(side_effect=block)
        with self.assertRaises(ValueError):await service.download('42',2,2)
        self.assertEqual(len(self.store.tasks()),1)

    def test_schema_eleven_migration_preserves_history_and_removal_marker(self):
        _,task=self.book()
        self.store.connection.execute('UPDATE manga SET removed_at=123 WHERE id=?',(task['manga_id'],))
        self.store.connection.executescript('DROP INDEX task_created_order; ALTER TABLE tasks DROP COLUMN assigned_node_id; ALTER TABLE tasks DROP COLUMN assigned_node_label; ALTER TABLE tasks DROP COLUMN notify_telegram; DROP TABLE report_deliveries; DROP TABLE manga_reports; DROP TABLE manga_likes; PRAGMA user_version=11;')
        self.store.close();self.store=Store(self.path)
        self.assertEqual(self.store.connection.execute('PRAGMA user_version').fetchone()[0],14)
        self.assertEqual(self.store.get(task['id'])['user_id'],2)
        self.assertEqual(self.store.connection.execute('SELECT removed_at FROM manga').fetchone()[0],123)
        self.store.close();self.store=Store(self.path)
        self.assertEqual(self.store.connection.execute('PRAGMA foreign_key_check').fetchall(),[])
