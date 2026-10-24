import asyncio
from pathlib import Path
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from jmemby.db.store import Store
from jmemby.domain.models import Chapter, Manga
from jmemby.services.channel_push import ChannelPushService, announcement_text
from jmemby.services.library import LibraryService
from jmemby.services.users import UserService


class ChannelPushTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / 'db'
        self.store = Store(self.path)
        self.users = UserService(self.store, {1})
        self.users.register(2, 'private_reader', 'Private reader')
        self.chat = SimpleNamespace(id=-1001, type='channel', title='Updates')
        self.member = SimpleNamespace(status='administrator', can_post_messages=True)
        self.bot = SimpleNamespace(id=99, get_chat=AsyncMock(return_value=self.chat),
                                   get_chat_member=AsyncMock(return_value=self.member),
                                   send_message=AsyncMock(return_value=SimpleNamespace(message_id=55)))
        self.service = ChannelPushService(self.store, self.users, self.bot, 'https://read.example.com')
        self.body = {'enabled': True, 'chat_id': '@updates', 'include_batch': False}

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def task(self, number=42, silent=False, completed=True):
        manga = Manga('jmcomic', str(number), 'Book <b>title</b>', ('Author',), (Chapter(str(number), 1, 'Chapter', ()),))
        task = self.store.submit(manga, 2, 2, notify_telegram=not silent)
        if completed:
            self.complete(task)
        return task

    def complete(self, task):
        for chapter in self.store.chapters(task['id']):
            self.store.update_chapter(chapter['id'], status='IMPORTED', komga_id='book')
        self.store.finish(task['id'], 'COMPLETED')

    def notices(self):
        return [dict(r) for r in self.store.connection.execute('SELECT * FROM channel_deliveries ORDER BY id')]

    async def test_disabled_by_default_and_no_history_backfill(self):
        self.task()
        self.assertFalse(self.service.settings(1)['enabled'])
        self.assertEqual(self.notices(), [])
        await self.service.save(1, self.body)
        await self.service.deliver_once()
        self.bot.send_message.assert_not_awaited()
        self.task(43)
        await self.service.deliver_once()
        self.bot.send_message.assert_awaited_once()

    async def test_success_records_once_and_keeps_private_notification_separate(self):
        saved = await self.service.save(1, self.body)
        self.assertEqual(saved['channel'], {'chat_id': -1001, 'title': 'Updates'})
        self.bot.send_message.assert_not_awaited()  # Validation does not publish anything.
        task = self.task()
        self.store.finish(task['id'], 'COMPLETED')
        self.assertEqual(len(self.notices()), 1)
        await self.service.deliver_once()
        await self.service.deliver_once()
        self.bot.send_message.assert_awaited_once()
        call = self.bot.send_message.call_args
        self.assertEqual(call.args[0], -1001)
        for text in ('已入库', 'Book <b>title</b>', 'Author', '漫画 ID：42', '章节：1', 'https://read.example.com'):
            self.assertIn(text, call.args[1])
        self.assertNotIn('private_reader', call.args[1])
        self.assertIsNone(call.kwargs['parse_mode'])
        self.assertTrue(call.kwargs['link_preview_options']['is_disabled'])
        self.assertEqual((self.notices()[0]['status'], self.notices()[0]['message_id']), ('SENT', 55))
        self.assertEqual(len(self.store.notifications()), 1)

    async def test_only_completed_imports_are_sent(self):
        await self.service.save(1, self.body)
        for i, state in enumerate(('FAILED', 'CANCELLED')):
            task = self.task(i+1, completed=False)
            self.store.finish(task['id'], state)
        task = self.task(completed=False)
        self.store.update(task['id'], status='IMPORT_PENDING')
        await self.service.deliver_once()
        self.bot.send_message.assert_not_awaited()
        self.assertEqual(self.notices(), [])
        self.complete(task)
        await self.service.deliver_once()
        self.bot.send_message.assert_awaited_once()

    async def test_batch_silence_defaults_and_explicit_channel_opt_in(self):
        await self.service.save(1, self.body)
        self.task(silent=True)
        self.assertEqual(self.notices(), [])
        await self.service.save(1, {**self.body, 'include_batch': True})
        self.task(43, silent=True)
        await self.service.deliver_once()
        self.bot.send_message.assert_awaited_once()
        self.assertEqual(self.store.notifications(), [])
        self.task(44, silent=True)
        await self.service.save(1, self.body)
        self.assertEqual(self.notices()[-1]['status'], 'SKIPPED')
        await self.service.deliver_once()
        self.bot.send_message.assert_awaited_once()

    async def test_network_failure_retries_after_restart_without_changing_task(self):
        await self.service.save(1, self.body)
        task = self.task()
        self.bot.send_message.side_effect = RuntimeError('secret token in transport error')
        await self.service.deliver_once()
        notice = self.notices()[0]
        self.assertEqual((notice['status'], notice['attempts'], notice['last_error']), ('PENDING', 1, 'RuntimeError'))
        self.assertGreater(notice['next_at'], time.time())
        self.assertEqual(self.store.get(task['id'])['status'], 'COMPLETED')
        self.store.close()
        self.store = Store(self.path)
        self.service = ChannelPushService(self.store, UserService(self.store, {1}), self.bot)
        self.bot.send_message.side_effect = None
        await self.service.deliver_once()
        self.assertEqual(self.bot.send_message.await_count, 1)
        self.store.connection.execute('UPDATE channel_deliveries SET next_at=0')
        await self.service.deliver_once()
        self.assertEqual(self.notices()[0]['status'], 'SENT')
        self.assertIsNone(self.notices()[0]['last_error'])
        self.assertEqual(self.bot.send_message.await_count, 2)

    async def test_rate_limit_pauses_the_channel_not_just_one_record(self):
        await self.service.save(1, self.body)
        self.task()
        self.task(43)
        error = RuntimeError('rate limited')
        error.retry_after = 120
        self.bot.send_message.side_effect = error
        await self.service.deliver_once()
        await self.service.deliver_once()
        self.bot.send_message.assert_awaited_once()
        self.assertGreater(self.notices()[0]['next_at'], time.time()+110)
        self.assertEqual(self.notices()[1]['attempts'], 0)

    async def test_concurrent_senders_do_not_duplicate_and_cancel_keeps_pending(self):
        await self.service.save(1, self.body)
        self.task()
        await asyncio.gather(self.service.deliver_once(), self.service.deliver_once())
        self.bot.send_message.assert_awaited_once()
        self.task(43)
        self.bot.send_message.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.service.deliver_once()
        self.assertEqual(self.notices()[-1]['status'], 'PENDING')

    async def test_disable_and_channel_change_skip_old_pending_without_redirect(self):
        await self.service.save(1, self.body)
        self.task()
        self.chat.id = -1002
        await self.service.save(1, self.body)
        self.assertEqual((self.notices()[0]['status'], self.notices()[0]['chat_id']), ('SKIPPED', -1001))
        self.task(43)
        self.bot.get_chat.side_effect = RuntimeError('offline')
        disabled = await self.service.save(1, {'enabled': False})
        self.assertFalse(disabled['enabled'])
        self.assertTrue(all(n['status'] == 'SKIPPED' for n in self.notices()))
        self.bot.get_chat.side_effect = None
        await self.service.save(1, self.body)
        await self.service.deliver_once()
        self.bot.send_message.assert_not_awaited()
        self.task(44)
        await self.service.deliver_once()
        self.assertEqual(self.bot.send_message.call_args.args[0], -1002)

    async def test_removed_or_missing_books_are_not_announced(self):
        await self.service.save(1, self.body)
        task = self.task()
        library = LibraryService(self.store, self.users, self.root, SimpleNamespace(scan=AsyncMock()))
        library.remove_files(1, task['manga_id'])
        self.assertEqual(self.notices()[0]['status'], 'SKIPPED')
        await self.service.deliver_once()
        other = self.task(43)
        self.store.update_chapter(self.store.chapters(other['id'])[0]['id'], status='MISSING')
        await self.service.deliver_once()
        self.bot.send_message.assert_not_awaited()
        self.assertTrue(all(n['status'] == 'SKIPPED' for n in self.notices()))

    async def test_permissions_input_validation_and_failed_save_preserve_settings(self):
        for actor in (2, 99):
            with self.assertRaises(ValueError):
                self.service.settings(actor)
            with self.assertRaises(ValueError):
                await self.service.save(actor, self.body)
        for body in ([], {}, {'enabled': 'yes'}, {**self.body, 'include_batch': 1}, {**self.body, 'chat_id': 42}):
            with self.assertRaises(ValueError):
                await self.service.save(1, body)
        original = self.service.config()
        for kind in ('group', 'supergroup', 'private'):
            self.chat.type = kind
            with self.assertRaises(ValueError):
                await self.service.save(1, self.body)
        self.chat.type = 'channel'
        self.member.can_post_messages = False
        with self.assertRaisesRegex(ValueError, '发布消息'):
            await self.service.save(1, self.body)
        self.assertEqual(self.service.config(), original)
        self.bot.get_chat.side_effect = RuntimeError('private details')
        with self.assertRaises(ValueError) as raised:
            await self.service.save(1, self.body)
        self.assertNotIn('private details', str(raised.exception))
        self.bot.send_message.assert_not_awaited()

    async def test_finish_and_outbox_are_atomic(self):
        await self.service.save(1, self.body)
        task = self.task(completed=False)
        self.store.connection.executescript("""
            CREATE TRIGGER reject_channel_insert BEFORE INSERT ON channel_deliveries
            BEGIN SELECT RAISE(ABORT, 'test failure'); END;
        """)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.finish(task['id'], 'COMPLETED')
        self.assertEqual(self.store.get(task['id'])['status'], 'QUEUED')
        self.assertEqual(self.store.notifications(), [])
        self.assertEqual(self.notices(), [])

    async def test_schema14_upgrade_preserves_history_without_sending_it(self):
        task = self.task()
        before = self.store.get(task['id'])
        self.store.connection.executescript('DROP TABLE channel_deliveries; PRAGMA user_version=14;')
        for _ in range(2):
            self.store.close()
            self.store = Store(self.path)
            self.assertEqual(self.store.connection.execute('PRAGMA user_version').fetchone()[0], 15)
            self.assertEqual(self.store.get(task['id']), before)
            self.assertEqual(self.notices(), [])
            self.assertEqual(self.store.connection.execute('PRAGMA foreign_key_check').fetchall(), [])

    async def test_text_length_is_bounded_for_unicode_metadata(self):
        from dataclasses import asdict
        import json
        manga = Manga('jmcomic', '42', '😀'*2000, ('😀'*2000,), (Chapter('52', 1, 'C', ()),))
        text = announcement_text({'snapshot': json.dumps(asdict(manga)), 'id': 'a'*32}, 'https://read.test/'+'x'*3000)
        self.assertLessEqual(len(text.encode('utf-16-le'))//2, 4096)
