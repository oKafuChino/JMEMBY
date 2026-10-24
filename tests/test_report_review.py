from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from jmemby.db.store import Store
from jmemby.domain.models import Chapter, Manga
from jmemby.services.community import CommunityService
from jmemby.services.library import LibraryService
from jmemby.services.users import UserService
from jmemby.bot.reports import deliver_reports


class ReportReviewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.path=self.root/'db'
        self.store=Store(self.path);self.users=UserService(self.store,{1,9})
        for uid in (2,3):self.users.register(uid,'reader'+str(uid),'User '+str(uid))
        self.community=CommunityService(self.store,self.users)
        self.manga=Manga('jmcomic','42','Book_%',(),(Chapter('52',1,'Chapter',()),))
        self.task=self.store.submit(self.manga,2,2)
        record=self.store.chapters(self.task['id'])[0]
        self.file=self.root/'library'/record['path'];self.file.parent.mkdir(parents=True);self.file.write_bytes(b'fixture')
        self.store.update_chapter(record['id'],status='IMPORTED');self.store.finish(self.task['id'],'COMPLETED')
        self.komga=SimpleNamespace(scan=AsyncMock())
        self.library=LibraryService(self.store,self.users,self.root,self.komga)
        self.community.report(2,'42');self.community.report(3,'42')

    def tearDown(self):
        self.store.close();self.temp.cleanup()

    def test_pending_list_search_filter_and_pagination(self):
        for uid in range(10,32):
            self.users.register(uid);self.community.report(uid,'42')
        first=self.community.review_list(1)
        self.assertEqual((first['pending'],first['total'],len(first['reports'])),(24,24,20))
        self.assertEqual(len(self.community.review_list(1,2)['reports']),4)
        self.assertEqual(self.community.review_list(1,query='_%')['total'],24)
        self.assertEqual(self.community.review_list(1,query='31')['total'],1)
        self.assertEqual(self.community.review_list(1,query="' OR 1=1 --")['total'],0)
        report=first['reports'][0]
        self.community.dismiss_report(1,report['id'])
        self.assertEqual(self.community.review_list(1)['total'],23)
        self.assertEqual(self.community.review_list(1,status='DISMISSED')['total'],1)
        self.assertEqual(self.community.review_list(1,status='all')['total'],24)

    async def test_ignore_preserves_files_and_suppresses_pending_reminders(self):
        report=self.community.review_list(1)['reports'][0]
        self.community.dismiss_report(1,report['id'])
        row=self.community.review_list(1,status='DISMISSED')['reports'][0]
        self.assertEqual(row['handled_by'],1);self.assertIsNotNone(row['handled_at'])
        self.assertTrue(self.file.exists())
        with self.assertRaises(ValueError):self.community.dismiss_report(9,report['id'])
        with self.assertRaises(ValueError):self.community.review_target(9,report['id'])
        bot=SimpleNamespace(send_message=AsyncMock())
        await deliver_reports(bot,self.community)
        self.assertEqual(bot.send_message.await_count,2)  # Only the other report, once per admin.
        self.assertTrue(all('举报管理' in call.args[1] for call in bot.send_message.call_args_list))
        self.assertTrue(all(f"#{report['id']}\n" not in call.args[1] for call in bot.send_message.call_args_list))

    async def test_remove_closes_all_pending_reports_before_scan(self):
        async def scan():
            self.assertEqual(self.community.review_list(1)['pending'],0)
            self.assertEqual(self.community.pending_reports(),[])
            raise RuntimeError('scan offline')
        self.komga.scan.side_effect=scan
        result=await self.library.remove(1,self.task['manga_id'])
        self.assertTrue(result['warning']);self.assertFalse(self.file.exists())
        rows=self.community.review_list(1,status='RESOLVED')['reports']
        self.assertEqual(len(rows),2)
        self.assertTrue(all(r['handled_by']==1 and r['handled_at'] for r in rows))
        self.store.close();self.store=Store(self.path)
        reopened=CommunityService(self.store,UserService(self.store,{1,9}))
        self.assertEqual(reopened.review_list(1)['pending'],0)
        self.assertEqual(reopened.review_list(1,status='RESOLVED')['total'],2)

    async def test_active_download_or_file_error_keeps_report_pending(self):
        self.store.update(self.task['id'],status='DOWNLOADING')
        with self.assertRaises(ValueError):await self.library.remove(1,self.task['manga_id'])
        self.assertTrue(all(r['busy'] for r in self.community.review_list(1)['reports']))
        self.store.update(self.task['id'],status='COMPLETED')
        with patch.object(Path,'unlink',side_effect=PermissionError()):
            with self.assertRaises(ValueError):await self.library.remove(1,self.task['manga_id'])
        self.assertEqual(self.community.review_list(1)['pending'],2)
        self.assertTrue(self.file.exists());self.komga.scan.assert_not_awaited()

    def test_review_is_admin_only_and_invalid_inputs_cannot_change_status(self):
        for actor in (2,55):
            for call in (lambda:self.community.review_list(actor),lambda:self.community.dismiss_report(actor,1)):
                with self.assertRaises(ValueError):call()
        for rid in (True,0,-1,None,[],999):
            with self.assertRaises(ValueError):self.community.dismiss_report(1,rid)
        for kwargs in ({'page':0},{'status':'bad'},{'query':'x'*201}):
            with self.assertRaises(ValueError):self.community.review_list(1,**kwargs)
        self.assertEqual(self.community.review_list(1)['pending'],2)

    def test_schema_twelve_upgrade_preserves_reports_and_old_task_delivery(self):
        self.store.connection.executescript('''
            DROP INDEX task_created_order; ALTER TABLE tasks DROP COLUMN assigned_node_id; ALTER TABLE tasks DROP COLUMN assigned_node_label; ALTER TABLE tasks DROP COLUMN notify_telegram;
            DROP INDEX review_reports;
            ALTER TABLE manga_reports DROP COLUMN status;
            ALTER TABLE manga_reports DROP COLUMN handled_by;
            ALTER TABLE manga_reports DROP COLUMN handled_at;
            PRAGMA user_version=12;
        ''')
        self.store.close();self.store=Store(self.path)
        self.community=CommunityService(self.store,UserService(self.store,{1,9}))
        self.assertEqual(self.store.connection.execute('PRAGMA user_version').fetchone()[0],14)
        self.assertEqual(self.community.review_list(1)['pending'],2)
        self.assertEqual(self.store.get(self.task['id'])['notify_telegram'],1)
        self.assertEqual(len(self.store.notifications()),1)
        self.store.close();self.store=Store(self.path)
        self.assertEqual(self.store.connection.execute('PRAGMA foreign_key_check').fetchall(),[])
