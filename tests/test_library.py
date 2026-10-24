from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from jmemby.db.store import Store
from jmemby.domain.models import Chapter, Manga
from jmemby.services.library import LibraryService
from jmemby.services.users import UserService


class LibraryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / 'db'
        self.store = Store(self.path)
        self.users = UserService(self.store, {1})
        self.users.register(2)
        self.komga = SimpleNamespace(scan=AsyncMock())
        self.coordinator = SimpleNamespace(books={})
        self.library = LibraryService(self.store, self.users, self.root, self.komga, self.coordinator)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def book(self, number=42, title='漫画', complete=True):
        manga = Manga('jmcomic', str(number), title, (), (Chapter(str(number), 1, 'Chapter', ()),))
        task = self.store.submit(manga, 2, -100)
        record = self.store.chapters(task['id'])[0]
        target = self.root / 'library' / record['path']
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'owned CBZ fixture')
        self.store.update_chapter(record['id'], status='IMPORTED', size=target.stat().st_size, checksum='hash', komga_id='book')
        if complete:
            self.store.finish(task['id'], 'COMPLETED')
        return manga, task, target

    async def test_remove_preserves_history_and_allows_redownload(self):
        manga, task, target = self.book()
        with self.assertRaises(ValueError):self.store.submit(manga, 2, 2)
        unrelated = target.parent / 'keep.txt';unrelated.write_text('keep')
        result = await self.library.remove(1, task['manga_id'])
        self.assertTrue(result['ok']);self.assertIsNone(result['warning'])
        self.komga.scan.assert_awaited_once()
        self.assertFalse(target.exists());self.assertTrue(unrelated.exists())
        self.assertEqual(self.library.list(1)['total'], 0)
        original = self.store.get(task['id'])
        self.assertEqual((original['status'], original['user_id'], original['chat_id']), ('COMPLETED', 2, -100))
        record = self.store.chapters(task['id'])[0]
        self.assertEqual(record['status'], 'MISSING')
        self.assertIsNone(record['checksum']);self.assertIsNone(record['komga_id'])
        new = self.store.submit(manga, 2, 2)
        self.assertNotEqual(new['id'], task['id'])
        self.assertEqual(self.library.list(1)['total'], 1)
        self.store.finish(new['id'], 'COMPLETED')
        with self.assertRaises(ValueError):self.store.submit(manga, 2, 2)

    async def test_scan_failure_is_reported_after_successful_deletion(self):
        _, task, target = self.book()
        self.komga.scan.side_effect = RuntimeError('secret')
        result = await self.library.remove(1, task['manga_id'])
        self.assertTrue(result['warning']);self.assertNotIn('secret', result['warning'])
        self.assertFalse(target.exists())
        self.assertEqual(self.library.list(1)['total'], 0)

    def test_active_queued_retry_paused_and_cancellation_are_rejected(self):
        _, task, target = self.book(complete=False)
        for status in ('QUEUED','DOWNLOADING','PACKAGING','IMPORT_PENDING','RETRY_WAIT','PAUSED_DISK','CANCEL_REQUESTED'):
            self.store.update(task['id'], status=status)
            with self.assertRaises(ValueError):self.library.remove_files(1, task['manga_id'])
            self.assertTrue(target.exists())
        self.store.finish(task['id'], 'CANCELLED')
        self.coordinator.books['node'] = {'task_id':task['id']}
        with self.assertRaises(ValueError):self.library.remove_files(1, task['manga_id'])
        self.coordinator.books.clear()
        self.library.remove_files(1, task['manga_id'])
        self.assertFalse(target.exists())

    def test_missing_file_and_repeat_removal_are_safe(self):
        _, task, target = self.book()
        target.unlink()
        self.library.remove_files(1, task['manga_id'])
        self.library.remove_files(1, task['manga_id'])
        self.assertEqual(self.library.list(1)['total'], 0)

    def test_delete_failure_retains_record_for_retry(self):
        _, task, target = self.book()
        with patch.object(Path, 'unlink', side_effect=PermissionError('secret')):
            with self.assertRaisesRegex(ValueError, '重试'):
                self.library.remove_files(1, task['manga_id'])
        self.assertTrue(target.exists())
        self.assertEqual(self.library.list(1)['total'], 1)
        self.library.remove_files(1, task['manga_id'])
        self.assertEqual(self.library.list(1)['total'], 0)

    def test_partial_delete_can_retry_without_losing_history(self):
        _, task, target = self.book()
        second = target.parent / 'second.cbz';second.write_bytes(b'second')
        self.store.connection.execute("INSERT INTO chapters(manga_id,source_id,ordinal,title,path) VALUES (?,?,?,?,?)",
            (task['manga_id'],'second',2,'Second',second.relative_to(self.library.root).as_posix()))
        original = Path.unlink
        def unlink(path, **kwargs):
            if path == second:
                raise PermissionError('denied')
            return original(path, **kwargs)
        with patch.object(Path, 'unlink', unlink):
            with self.assertRaises(ValueError):self.library.remove_files(1, task['manga_id'])
        self.assertFalse(target.exists());self.assertTrue(second.exists())
        self.assertEqual(self.library.list(1)['total'], 1)
        self.library.remove_files(1, task['manga_id'])
        self.assertFalse(second.exists())
        self.assertEqual(self.store.get(task['id'])['status'], 'COMPLETED')

    def test_paths_must_be_owned_cbz_inside_expected_directory(self):
        _, task, target = self.book()
        record = self.store.chapters(task['id'])[0]
        for path in ('../outside.cbz', str(self.root/'outside.cbz'), 'other/file.cbz', record['path']+'.txt'):
            self.store.connection.execute('UPDATE chapters SET path=? WHERE id=?', (path, record['id']))
            with self.assertRaises(ValueError):self.library.remove_files(1, task['manga_id'])
            self.assertTrue(target.exists())
        self.store.connection.execute('UPDATE chapters SET path=? WHERE id=?', (record['path'], record['id']))
        with patch.object(Path, 'is_symlink', return_value=True):
            with self.assertRaises(ValueError):self.library.remove_files(1, task['manga_id'])
        self.assertTrue(target.exists())

    def test_all_paths_are_validated_before_unlink(self):
        _, task, target = self.book()
        self.store.connection.execute("INSERT INTO chapters(manga_id,source_id,ordinal,title,path) VALUES (?,?,?,?,?)",
                                      (task['manga_id'],'bad',2,'bad','../outside.cbz'))
        with self.assertRaises(ValueError):self.library.remove_files(1, task['manga_id'])
        self.assertTrue(target.exists())

    def test_real_symlink_cannot_delete_its_target(self):
        _, task, target = self.book()
        outside = self.root / 'outside.cbz';outside.write_bytes(b'keep outside')
        target.unlink()
        try:
            target.symlink_to(outside)
        except OSError:
            self.skipTest('creating symlinks requires platform permission')
        with self.assertRaises(ValueError):self.library.remove_files(1, task['manga_id'])
        self.assertEqual(outside.read_bytes(), b'keep outside')
        self.assertTrue(target.is_symlink())

    async def test_bridge_scan_requires_explicit_acceptance(self):
        from jmemby.integrations.komga.bridge import KomgaBridge
        bridge=KomgaBridge('["unused"]',1)
        with patch.object(bridge,'request',new=AsyncMock(return_value={'ok':True})) as request:
            await bridge.scan()
            request.assert_awaited_once_with({'operation':'scan'})
        with patch.object(bridge,'request',new=AsyncMock(return_value={})):
            with self.assertRaises(ValueError):await bridge.scan()

    def test_list_search_pagination_and_authorization(self):
        for n in range(23):self.book(n, '匹配_%' if n != 22 else 'Other')
        result=self.library.list(1, query='_%')
        self.assertEqual(result['total'], 22);self.assertEqual(len(result['manga']), 20)
        self.assertEqual(len(self.library.list(1, 2, '_%')['manga']), 2)
        self.assertEqual(self.library.list(1, query='22')['total'], 1)
        self.assertEqual(self.library.list(1, query="' OR 1=1 --")['total'], 0)
        for call in (lambda:self.library.list(2),lambda:self.library.remove_files(2,1),
                     lambda:self.library.list(1,0),lambda:self.library.list(1,query='x'*201),
                     lambda:self.library.remove_files(1,True),lambda:self.library.remove_files(1,999)):
            with self.assertRaises(ValueError):call()

    def test_schema_ten_migration_and_removal_survive_restart(self):
        manga, task, _ = self.book()
        self.store.connection.executescript('DROP TABLE channel_deliveries; DROP INDEX task_created_order; ALTER TABLE tasks DROP COLUMN assigned_node_id; ALTER TABLE tasks DROP COLUMN assigned_node_label; ALTER TABLE tasks DROP COLUMN notify_telegram; DROP TABLE report_deliveries; DROP TABLE manga_reports; DROP TABLE manga_likes; ALTER TABLE manga DROP COLUMN removed_at; PRAGMA user_version=10;')
        self.store.close();self.store=Store(self.path)
        self.users=UserService(self.store,{1})
        self.library=LibraryService(self.store,self.users,self.root,self.komga)
        self.assertEqual(self.store.connection.execute('PRAGMA user_version').fetchone()[0],15)
        with self.assertRaises(ValueError):self.store.submit(manga,2,2)
        self.library.remove_files(1,task['manga_id'])
        self.store.close();self.store=Store(self.path)
        self.store.submit(manga,2,2)
