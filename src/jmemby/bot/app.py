import asyncio
import json
import shutil
import time

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from jmemby.domain.errors import JobError


def allowed(event, ids):
    user = getattr(event, "from_user", None)
    message = event.message if isinstance(event, CallbackQuery) else event
    chat = getattr(message, "chat", None)
    return bool(user and user.id in ids and chat and chat.type == "private" and chat.id == user.id)


class Authorization(BaseMiddleware):
    def __init__(self, ids):
        self.ids = ids

    async def __call__(self, handler, event, data):
        if not allowed(event, self.ids):
            if isinstance(event, CallbackQuery):
                await event.answer("无权操作", show_alert=True)
            return
        return await handler(event, data)


def describe(task):
    title = json.loads(task["snapshot"])["title"][:200]
    return (f"{title}\n任务：{task['id']}\n状态：{task['status']}\n"
            f"{task['progress']}\n{task['error'] or ''}")[:3500]


def create_dispatcher(service, settings):
    router = Router()
    guard = Authorization(settings.allowed_user_ids)
    router.message.outer_middleware(guard)
    router.callback_query.outer_middleware(guard)

    @router.message(F.text)
    async def message_handler(message: Message):
        text = message.text.strip()
        head, _, argument = text.partition(" ")
        command = head.split("@")[0]
        user_id = message.from_user.id
        try:
            if command in {"/start", "/help"}:
                await message.answer("发送数字漫画 ID 或 /download ID\n/queue [页码] /status 任务ID\n/cancel 任务ID /retry 任务ID\n/library /disk")
            elif command == "/library":
                await message.answer(settings.komga_public_url or "尚未配置访问地址")
            elif command == "/disk":
                free = shutil.disk_usage(settings.storage_root).free / 1024**3
                await message.answer(f"剩余 {free:.1f} GiB；暂停阈值 {settings.min_free_space_gb} GiB")
            elif command == "/queue":
                page = max(0, int(argument or "1") - 1)
                tasks = service.store.tasks(user_id, page)
                await message.answer(("\n\n".join(describe(t) for t in tasks) or "暂无任务")[:4000])
            elif command == "/status":
                await message.answer(describe(service.get(argument.strip(), user_id)))
            elif command == "/cancel":
                service.store.request_cancel(argument.strip(), user_id)
                await message.answer("已请求取消；停止后会通知，已发布文件保留")
            elif command == "/retry":
                await message.answer(describe(service.retry(argument.strip(), user_id)))
            elif command.startswith("/") and command != "/download":
                await message.answer("未知命令，请使用 /help")
            else:
                manga, token = await service.preview(argument if command == "/download" else text, user_id, message.chat.id)
                keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="确认下载", callback_data=f"confirm:{token}"),
                    InlineKeyboardButton(text="取消", callback_data=f"dismiss:{token}"),
                ]])
                await message.answer(f"{manga.title[:500]}\n作者：{', '.join(manga.authors)[:500]}\n"
                                     f"来源 ID：{manga.source_id}\n章节：{len(manga.chapters)}", reply_markup=keyboard)
        except (ValueError, JobError) as exc:
            await message.answer(str(exc)[:1000])

    @router.callback_query()
    async def callback_handler(callback: CallbackQuery):
        try:
            action, token = (callback.data or "").split(":", 1)
            if action == "dismiss":
                service.store.dismiss(token, callback.from_user.id)
                await callback.answer("已取消")
            elif action == "confirm":
                task = service.confirm(token, callback.from_user.id)
                if task["user_id"] != callback.from_user.id:
                    await callback.answer("该漫画已有下载任务")
                    return
                service.store.update(task["id"], message_id=callback.message.message_id)
                await callback.answer("已加入队列")
                await callback.message.edit_text(describe(task))
            else:
                await callback.answer("无效操作")
        except (ValueError, JobError) as exc:
            await callback.answer(str(exc)[:180], show_alert=True)
        except TelegramBadRequest:
            pass

    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    return dispatcher


async def deliver_notifications(bot: Bot, store, public_url):
    for notice in store.notifications():
        task = store.get(notice["task_id"])
        text = describe(task)
        if task["status"] == "COMPLETED" and public_url:
            text += "\n" + public_url
        try:
            await bot.send_message(task["chat_id"], text)
            store.notified(notice["id"])
        except TelegramRetryAfter as exc:
            store.defer_notification(notice["id"], exc.retry_after)
        except Exception:
            store.defer_notification(notice["id"], min(3600, 2 ** min(notice["attempts"] + 1, 11)))


async def notifications(bot, store, public_url):
    last_text = {}
    while True:
        await deliver_notifications(bot, store, public_url)
        for task in store.tasks(active=True):
            if not task["message_id"]:
                continue
            text = describe(task)
            if last_text.get(task["id"]) == text:
                continue
            try:
                await bot.edit_message_text(text=text, chat_id=task["chat_id"], message_id=task["message_id"])
                last_text[task["id"]] = text
            except TelegramRetryAfter as exc:
                await asyncio.sleep(min(exc.retry_after, 60))
                break
            except TelegramBadRequest:
                store.update(task["id"], message_id=None)
            except Exception:
                pass
        await asyncio.sleep(5)
