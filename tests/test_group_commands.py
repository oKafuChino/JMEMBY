import importlib.util
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from jmemby.bot.access import chat_allows, parse_command
from jmemby.db.store import Store
from jmemby.domain.models import Chapter, Manga
from jmemby.services.tasks import TaskService
from jmemby.services.users import UserService


def manga():
    return Manga("jmcomic", "42", "Test", ("Author",), (Chapter("52", 1, "Chapter", ()),))


class AccessTests(unittest.TestCase):
    def test_command_arguments_accept_whitespace_and_bot_mentions(self):
        for text in ("/jm 42", " /jm\n42 ", "/jm\t42"):
            self.assertEqual(parse_command(text), ("/jm", "", "42"))
        self.assertEqual(parse_command("/jm@TestBot 42"), ("/jm", "TestBot", "42"))
        self.assertEqual(parse_command("/jm"), ("/jm", "", ""))

    def test_groups_only_accept_jm_and_download_buttons(self):
        for kind in ("group", "supergroup"):
            self.assertTrue(chat_allows(kind, -100, 1, "/jm"))
            for command in ("/download", "/register", "/account", "/admin", "/queue", "/help", "42", "📋 我的任务", "/jmfoo"):
                self.assertFalse(chat_allows(kind, -100, 1, command))
            for action in ("confirm", "dismiss"):
                self.assertTrue(chat_allows(kind, -100, 1, action=action))
            for action in ("sched", "account", "", "unknown"):
                self.assertFalse(chat_allows(kind, -100, 1, action=action))

    def test_private_identity_and_channel_boundaries(self):
        self.assertTrue(chat_allows("private", 1, 1, "/account"))
        self.assertFalse(chat_allows("private", 2, 1, "/jm"))
        self.assertFalse(chat_allows("channel", -100, 1, "/jm"))


class ConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.directory.name) / "db.sqlite")
        self.users = UserService(self.store, {1})
        self.service = TaskService(self.store, None, users=self.users)
        self.token = self.store.confirmation(manga(), 1, -100)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def test_confirm_is_bound_to_owner_and_chat(self):
        for user_id, chat_id in ((2, -100), (1, -200), (1, 1)):
            with self.assertRaises(ValueError):
                self.store.confirm(self.token, user_id, chat_id)
        self.assertEqual(self.store.tasks(), [])
        task = self.service.confirm(self.token, 1, -100)
        self.assertEqual(task["chat_id"], -100)
        self.assertEqual(task["id"], self.service.confirm(self.token, 1, -100)["id"])
        with self.assertRaises(ValueError):
            self.service.confirm(self.token, 1, -200)

    def test_dismiss_cannot_be_used_by_another_member_or_chat(self):
        for user_id, chat_id in ((2, -100), (1, -200)):
            with self.assertRaises(ValueError):
                self.store.dismiss(self.token, user_id, chat_id)
        self.assertFalse(self.store.connection.execute("SELECT dismissed FROM confirmations").fetchone()[0])
        self.store.dismiss(self.token, 1, -100)
        with self.assertRaisesRegex(ValueError, "已取消"):
            self.service.confirm(self.token, 1, -100)

    def test_confirmed_preview_cannot_be_dismissed(self):
        task = self.service.confirm(self.token, 1, -100)
        with self.assertRaisesRegex(ValueError, "任务已确认"):
            self.store.dismiss(self.token, 1, -100)
        self.assertEqual(self.store.get(task["id"])["status"], "QUEUED")

    def test_expired_group_preview_does_not_create_task(self):
        self.store.connection.execute("UPDATE confirmations SET expires=0")
        with self.assertRaisesRegex(ValueError, "过期"):
            self.service.confirm(self.token, 1, -100)
        self.assertEqual(self.store.tasks(), [])

    def test_cross_chat_duplicate_keeps_original_destination(self):
        first = self.store.submit(manga(), 1, 1)
        self.store.update(first["id"], message_id=99)
        with self.assertRaisesRegex(ValueError, "拒绝重复下载"):
            self.service.confirm(self.token, 1, -100)
        duplicate = self.store.get(first["id"])
        self.assertEqual((duplicate["id"], duplicate["chat_id"], duplicate["message_id"]), (first["id"], 1, 99))
        self.assertEqual(len(self.store.tasks()), 1)

    def test_private_retry_of_failed_group_task_uses_current_chat(self):
        first = self.service.confirm(self.token, 1, -100)
        self.store.finish(first["id"], "FAILED")
        retry = self.service.retry(first["id"], 1, 1)
        self.assertEqual(retry["chat_id"], 1)
        self.assertEqual(retry["retry_of"], first["id"])

    def test_group_confirmation_rechecks_membership(self):
        self.users.register(2)
        token = self.store.confirmation(manga(), 2, -100)
        self.users.set_blocked(1, 2, True)
        with self.assertRaisesRegex(ValueError, "禁用"):
            self.service.confirm(token, 2, -100)
        self.assertEqual(self.store.tasks(), [])


HAS_AIOGRAM = importlib.util.find_spec("aiogram") is not None
if HAS_AIOGRAM:
    from datetime import datetime, timezone
    from aiogram import Bot
    from aiogram.types import CallbackQuery, Chat, Message, Update, User
    from jmemby.bot.app import create_dispatcher, deliver_notifications


@unittest.skipUnless(HAS_AIOGRAM, "aiogram is required for real dispatcher tests")
class GroupDispatcherTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.directory.name) / "db.sqlite")
        self.users = UserService(self.store, {1})
        self.users.register(2)
        self.provider = SimpleNamespace(get_manga=AsyncMock(return_value=manga()))
        self.service = TaskService(self.store, self.provider, users=self.users)
        self.bot = Bot("123456:ABCdef123456")
        self.dispatcher = create_dispatcher(self.service, SimpleNamespace(), None, None)
        self.pending = self.message(None, user_id=123456, is_bot=True)
        self.pending = self.pending.model_copy(update={"message_id": 99})
        self.answer = self.start_patch(Message, "answer", return_value=self.pending)
        self.edit = self.start_patch(Message, "edit_text")
        self.delete = self.start_patch(Message, "delete")
        self.alert = self.start_patch(CallbackQuery, "answer")
        self.get_me = self.start_patch(Bot, "get_me", return_value=User(id=123456, is_bot=True, first_name="Bot", username="TestBot"))

    def start_patch(self, cls, name, **kwargs):
        mocker = patch.object(cls, name, new_callable=AsyncMock, **kwargs)
        result = mocker.start()
        self.addCleanup(mocker.stop)
        return result

    async def asyncTearDown(self):
        await self.bot.session.close()
        self.store.close()
        self.directory.cleanup()

    def message(self, text, chat_id=-100, kind="supergroup", user_id=2, is_bot=False, sender_chat=None):
        return Message(message_id=10, date=datetime.now(timezone.utc), text=text,
                       from_user=User(id=user_id, is_bot=is_bot, first_name="Test"),
                       chat=Chat(id=chat_id, type=kind), sender_chat=sender_chat)

    async def send(self, text, **kwargs):
        message = self.message(text, **kwargs)
        self.pending = self.pending.model_copy(update={"chat": message.chat})
        self.answer.return_value = self.pending
        await self.dispatcher.feed_update(self.bot, Update(update_id=1, message=message))

    async def click(self, action, token, user_id=2, message=None):
        callback = CallbackQuery(id="click", from_user=User(id=user_id, is_bot=False, first_name="Test"),
                                 chat_instance="test", data=f"{action}:{token}", message=message or self.pending)
        await self.dispatcher.feed_update(self.bot, Update(update_id=2, callback_query=callback))

    def token(self):
        return self.store.connection.execute("SELECT token FROM confirmations ORDER BY rowid DESC LIMIT 1").fetchone()[0]

    async def test_jm_preview_confirmation_and_notification_in_both_group_types(self):
        for kind in ("group", "supergroup"):
            await self.send(f"/jm {self.provider.get_manga.return_value.source_id}", kind=kind)
            await self.click("confirm", self.token())
            task = self.store.tasks()[0]
            self.assertEqual((task["user_id"], task["chat_id"], task["message_id"]), (2, -100, 99))
            self.store.finish(task["id"], "COMPLETED")
            bot = SimpleNamespace(edit_message_text=AsyncMock())
            await deliver_notifications(bot, self.store, "")
            self.assertEqual(bot.edit_message_text.call_args.kwargs["chat_id"], -100)
            self.assertEqual(bot.edit_message_text.call_args.kwargs["message_id"], 99)
            self.provider.get_manga.return_value = Manga("jmcomic", "43", "Next", (), (Chapter("53", 1, "Chapter", ()),))

    async def test_other_group_messages_are_silently_ignored_even_for_admin(self):
        for text in ("/download 42", "/register", "/help", "/start", "/admin", "/account", "/logs", "/queue", "/retry x", "/cancel x", "/schedule_add", "🔑 阅读账号", "42", "https://example.com/album/42"):
            await self.send(text, user_id=1)
        self.answer.assert_not_awaited()
        self.provider.get_manga.assert_not_awaited()

    async def test_mention_only_matches_this_bot_and_missing_argument_shows_usage(self):
        await self.send("/jm@OtherBot 42")
        self.answer.assert_not_awaited()
        await self.send("/jm")
        self.assertIn("用法", self.answer.call_args.args[0])
        self.provider.get_manga.assert_not_awaited()
        await self.send("/jm@tEsTbOt\n42")
        self.provider.get_manga.assert_awaited_once_with("42")
        self.get_me.assert_awaited_once()

    async def test_private_jm_and_plain_id_work_but_download_is_removed(self):
        for text in ("/jm\t42", "42"):
            await self.send(text, kind="private", chat_id=2)
        self.assertEqual(self.provider.get_manga.await_count, 2)
        await self.send("/download 42", kind="private", chat_id=2)
        self.assertIn("未知命令", self.answer.call_args.args[0])
        self.assertEqual(self.provider.get_manga.await_count, 2)

    async def test_unregistered_blocked_anonymous_and_channel_senders_cannot_download(self):
        await self.send("/jm 42", user_id=3)
        self.assertIn("私聊", self.answer.call_args.args[0])
        self.users.set_blocked(1, 2, True)
        await self.send("/jm 42")
        self.assertIn("禁用", self.answer.call_args.args[0])
        self.answer.reset_mock()
        await self.send("/jm 42", user_id=1, sender_chat=Chat(id=-100, type="supergroup"))
        await self.send("/jm 42", user_id=1, kind="channel")
        await self.send("/jm 42", user_id=1, is_bot=True)
        self.answer.assert_not_awaited()
        self.provider.get_manga.assert_not_awaited()

    async def test_other_members_cannot_confirm_or_delete_preview(self):
        await self.send("/jm 42")
        for action in ("confirm", "dismiss"):
            await self.click(action, self.token(), user_id=1)
            self.assertTrue(self.alert.call_args.kwargs["show_alert"])
        self.assertEqual(self.store.tasks(), [])
        self.delete.assert_not_awaited()
        await self.click("dismiss", self.token())
        self.delete.assert_awaited_once()

    async def test_forwarded_buttons_and_other_callback_actions_are_rejected(self):
        await self.send("/jm 42")
        for action in ("confirm", "dismiss"):
            await self.click(action, self.token(), message=self.message(None, chat_id=-200))
        await self.click("sched", "old")
        self.assertEqual(self.store.tasks(), [])
        self.delete.assert_not_awaited()

    async def test_cross_chat_duplicate_does_not_overwrite_message_even_with_same_id(self):
        for message_id in (None, 99):
            await self.send("/jm 42")
            token = self.token()
            first = self.store.submit(manga(), 2, 2)
            self.store.update(first["id"], message_id=message_id)
            self.edit.reset_mock()
            await self.click("confirm", token)
            self.assertIn("拒绝重复下载", self.alert.call_args.args[0])
            task = self.store.get(first["id"])
            self.assertEqual((task["chat_id"], task["message_id"]), (2, message_id))
            self.edit.assert_not_awaited()
            self.delete.assert_not_awaited()
            self.store.finish(first["id"], "CANCELLED")


if __name__ == "__main__":
    unittest.main()
