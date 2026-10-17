"""Real aiogram routing regressions; no Telegram network requests."""
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from jmemby.db.store import Store
from jmemby.domain.schedule_tags import schedule_tags
from jmemby.services.schedules import ScheduleService
from jmemby.services.users import UserService
from test_schedules import Provider, timestamp

HAS_AIOGRAM = importlib.util.find_spec("aiogram") is not None
if HAS_AIOGRAM:
    from aiogram import Bot, Dispatcher, Router
    from aiogram.types import Chat, Message, Update, User
    from jmemby.bot.schedules import create_schedule_router


@unittest.skipUnless(HAS_AIOGRAM, "aiogram is required for real dispatcher tests")
class ScheduleRouterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "db.sqlite")
        self.now = timestamp("2026-10-18T02:00:00+08:00")
        self.service = ScheduleService(self.store, UserService(self.store, {1}), Provider(),
                                       self.temp.name, lambda: self.now)
        self.settings = SimpleNamespace(schedule_default_timezone="Asia/Hong_Kong",
                                        disk_space_check_enabled=False)
        self.dispatcher = Dispatcher()
        self.dispatcher.include_router(create_schedule_router(self.service, self.settings))
        self.fallback = AsyncMock()
        fallback = Router()
        fallback.message.register(self.fallback)
        self.dispatcher.include_router(fallback)
        self.bot = Bot("123456:ABCdef123456")
        self.answer_patch = patch.object(Message, "answer", new_callable=AsyncMock)
        self.answer = self.answer_patch.start()
        self.update_id = 0

    async def asyncTearDown(self):
        self.answer_patch.stop()
        await self.bot.session.close()
        self.store.close()
        self.temp.cleanup()

    async def send(self, text, user=1, chat=None, chat_type="private"):
        self.update_id += 1
        message = Message(message_id=self.update_id, date=datetime.now(timezone.utc), text=text,
                          from_user=User(id=user, is_bot=False, first_name="Test"),
                          chat=Chat(id=user if chat is None else chat, type=chat_type))
        await self.dispatcher.feed_update(self.bot, Update(update_id=self.update_id, message=message))

    def draft(self):
        return self.store.connection.execute("SELECT * FROM schedule_drafts WHERE owner_user_id=1").fetchone()

    async def test_tag_message_responds_and_completes_multitag_wizard(self):
        # SQLite uses its default thread check: a sync aiogram filter raises here.
        await self.send("/schedule_add")
        await self.send("冒险，科幻\n冒险")
        self.assertEqual(self.draft()["step"], "kind")
        self.assertEqual(json.loads(self.draft()["data"])["tags"], ["冒险", "科幻"])
        self.assertIn("once", self.answer.call_args.args[0])
        for text in ("daily", "03:00", "default", "2"):
            await self.send(text)
        rows = self.service.list(1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(schedule_tags(rows[0]), ["冒险", "科幻"])
        self.assertEqual(rows[0]["item_limit"], 2)
        self.assertEqual(rows[0]["enabled"], 0)
        self.assertIsNone(self.draft())
        self.assertEqual(self.answer.await_count, 6)
        self.fallback.assert_not_awaited()

    async def test_invalid_tag_can_be_corrected_and_cancel_releases_text(self):
        await self.send("/schedule_add")
        await self.send(",，")
        self.assertEqual(self.draft()["step"], "tag")
        self.assertIn("请输入", self.answer.call_args.args[0])
        await self.send("Sci Fi")
        self.assertEqual(json.loads(self.draft()["data"])["tags"], ["Sci Fi"])
        await self.send("/schedule_cancel")
        await self.send("12345")
        self.fallback.assert_awaited_once()

    async def test_expired_draft_explains_restart_and_is_removed(self):
        await self.send("/schedule_add")
        self.now += 601
        await self.send("冒险")
        self.assertIn("已过期", self.answer.call_args.args[0])
        self.assertIsNone(self.draft())
        self.fallback.assert_not_awaited()
        await self.send("12345")
        self.fallback.assert_awaited_once()

    async def test_legacy_in_progress_draft_finishes(self):
        data = {"tag": "Sci Fi, Fantasy", "kind": "daily", "time": "03:00", "timezone": "Asia/Hong_Kong"}
        self.store.connection.execute("INSERT INTO schedule_drafts VALUES (?,?,?,?)",
                                      (1, "limit", json.dumps(data), self.now + 600))
        await self.send("2")
        self.assertEqual(schedule_tags(self.service.list(1)[0]), ["Sci Fi, Fantasy"])

    async def test_private_admin_draft_cannot_be_changed_from_group_or_other_user(self):
        await self.send("/schedule_add")
        await self.send("科幻", chat=-100, chat_type="group")
        self.assertEqual(self.draft()["step"], "tag")
        self.assertIn("私聊", self.answer.call_args.args[0])
        await self.send("/schedule_add", user=2)
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM schedule_drafts").fetchone()[0], 1)
        await self.send("冒险", user=2)
        self.fallback.assert_awaited_once()
        self.assertEqual(self.draft()["step"], "tag")

    async def test_multitag_preview_uses_list_and_does_not_submit(self):
        schedule = self.service.create(1, ["冒险", "科幻"],
                                       {"kind": "daily", "time": "03:00", "timezone": "Asia/Hong_Kong"})
        with patch.object(self.service.provider, "search_tag", wraps=self.service.provider.search_tag) as search:
            await self.send(f"/schedule_preview {schedule['id']}")
        self.assertEqual([call.args[0] for call in search.await_args_list], ["冒险", "科幻"])
        self.assertIn("精确点赞", self.answer.call_args.args[0])
        self.assertEqual(len(self.store.tasks()), 0)


if __name__ == "__main__":
    unittest.main()
