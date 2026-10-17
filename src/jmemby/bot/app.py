import asyncio
import json
import shutil
import time

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, BufferedInputFile, InputMediaPhoto
from jmemby.bot.completion import completion_text, read_cover
from html import escape
from jmemby.bot.presentation import describe, normalize

from jmemby.domain.errors import JobError


def allowed(event, ids):
    user = getattr(event, "from_user", None)
    message = event.message if isinstance(event, CallbackQuery) else event
    chat = getattr(message, "chat", None)
    return bool(user and user.id in ids and chat and chat.type == "private" and chat.id == user.id)


class Authorization(BaseMiddleware):
    def __init__(self, users):
        self.users = users

    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if not user or not allowed(event, {user.id}):
            if isinstance(event, CallbackQuery):
                await event.answer("无权操作", show_alert=True)
            return
        command = (normalize(getattr(event, "text", "") or "").split() or [""])[0].split("@")[0]
        public = not isinstance(event, CallbackQuery) and command in {"/start", "/help", "/register", "/me"}
        if not public:
            try:
                self.users.require_member(user.id)
            except ValueError as exc:
                if isinstance(event, CallbackQuery):
                    await event.answer(str(exc), show_alert=True)
                else:
                    await event.answer(str(exc))
                return
        return await handler(event, data)


def create_dispatcher(service, settings, accounts, diagnostics):
    router = Router()
    users = service.users
    guard = Authorization(users)
    router.message.outer_middleware(guard)
    router.callback_query.outer_middleware(guard)

    def menu(user_id):
        rows = [["🏠 首页", "👤 我的账号"], ["📝 注册", "🔑 阅读账号"], ["📋 我的任务", "📚 阅读入口"]]
        if users.is_admin(user_id):
            rows.append(["⚙️ 管理面板", "👥 用户列表"])
            rows.append(["🩺 服务状态", "📜 最近日志"])
        return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=t) for t in row] for row in rows], resize_keyboard=True)

    async def reading_account(message, user_id):
        await message.answer("⏳ 正在准备你的阅读账号，请稍候……")
        try:
            account = await accounts.ensure(user_id)
        except JobError as exc:
            await message.answer(f"⚠️ Bot 注册已保留，阅读账号尚未开通。\n{exc}\n稍后点击「🔑 阅读账号」重试，不会重复消耗邀请码。")
            return
        text = f"📚 <b>你的 Komga 阅读账号</b>\n\n登录邮箱：<code>{escape(account['email'])}</code>"
        if account["password"]:
            text += f"\n初始密码：<code>{escape(account['password'])}</code>\n\n请保存密码，登录后在 Komga 修改。密码发送成功后不会在 Bot 中再次展示。"
        else:
            text += "\n\n初始密码已发送。忘记密码请联系管理员在 Komga 重置。"
        await message.answer(text, parse_mode="HTML", protect_content=True)
        accounts.delivered(user_id)
        if settings.komga_public_url:
            await message.answer("阅读入口：" + settings.komga_public_url)

    @router.message(F.text)
    async def message_handler(message: Message):
        text = normalize(message.text)
        head, _, argument = text.partition(" ")
        command = head.split("@")[0]
        user_id = message.from_user.id
        pending = None
        try:
            if command in {"/start", "/help"}:
                help_text = "📚 欢迎使用 JMEMBY\n\n① 注册账号 → ② 发送漫画 ID → ③ 确认下载\n\n/register [邀请码] 注册\n/account 领取或补开阅读账号\n/me 我的信息\n/queue 我的任务\n/status 任务ID 查看进度\n/cancel 任务ID 取消任务\n/retry 任务ID 重试\n/library 开始阅读"
                if users.invite_required():
                    help_text += "\n当前为邀请注册：请向管理员获取邀请码"
                if users.is_admin(user_id):
                    help_text += "\n/status 服务状态\n/logs 最近 100 条应用日志"
                    help_text += "\n管理员：/admin /users [页码]\n/registration on|off\n/ban 用户ID /unban 用户ID\n/disk 磁盘状态"
                    help_text += "\n/invite_mode on|off\n/invite_create [次数]\n/invites [页码]\n/invite_revoke 编号"
                await message.answer(help_text, reply_markup=menu(user_id))
            elif command == "/register":
                result = users.register(user_id, message.from_user.username, message.from_user.full_name, argument.strip())
                await message.answer("✅ " + result, reply_markup=menu(user_id))
                if not users.is_admin(user_id):
                    await reading_account(message, user_id)
            elif command == "/account":
                await reading_account(message, user_id)
            elif command == "/me":
                account = users.get(user_id)
                status = "管理员" if users.is_admin(user_id) else "未注册" if not account else "已禁用" if account["blocked"] else "已注册"
                reading = accounts.get(user_id)
                reading_status = "已开通" if reading and reading["status"] == "READY" else "待开通"
                await message.answer(f"👤 我的账号\n\n用户 ID：{user_id}\nBot 状态：{status}\n阅读账号：{reading_status}\n\n点击「🔑 阅读账号」领取或重试开通。", reply_markup=menu(user_id))
            elif command == "/admin":
                stats = users.statistics(user_id)
                await message.answer(
                    f"⚙️ 管理面板\n\n👥 注册总数：{stats['total']}\n✅ 正常：{stats['active']}  🚫 禁用：{stats['blocked']}\n"
                    f"🛡 管理员：{stats['admins']}\n\n📝 新注册：{'开放' if stats['registration_open'] else '关闭'}\n"
                    f"🎟 邀请注册：{'开启' if stats['invite_required'] else '关闭'}\n\n"
                    "/status 服务状态\n/logs 最近 100 条应用日志\n/users [页码] 查看用户\n/registration on|off 注册开关\n/invite_mode on|off 邀请模式\n"
                    "/invite_create [次数] 生成邀请码\n/invites [页码] 邀请记录\n/invite_revoke 编号 撤销邀请码\n"
                    "/ban 用户ID 禁用 Bot\n/unban 用户ID 解禁 Bot\n\nKomga 账号权限请在 Komga 管理。",
                    reply_markup=menu(user_id))
            elif command == "/users":
                members = users.list_users(user_id, int(argument or "1"))
                lines = [f"{u['user_id']} | @{u['username'] or '-'} | {'禁用' if u['blocked'] else '正常'}" for u in members]
                await message.answer("\n".join(lines) or "本页暂无注册用户")
            elif command == "/registration":
                users.require_admin(user_id)
                if argument.strip().lower() not in {"on", "off"}:
                    raise ValueError("用法：/registration on 或 /registration off")
                users.set_registration(user_id, argument.strip().lower() == "on")
                await message.answer("新用户注册已" + ("开放" if users.registration_open() else "关闭"))
            elif command == "/invite_mode":
                users.require_admin(user_id)
                mode = argument.strip().lower()
                if mode not in {"on", "off"}:
                    raise ValueError("用法：/invite_mode on 或 /invite_mode off")
                users.set_invite_required(user_id, mode == "on")
                await message.answer("邀请注册已" + ("开启：新用户必须使用 /register 邀请码" if mode == "on" else "关闭") + "；注册总开关保持不变")
            elif command == "/invite_create":
                users.require_admin(user_id)
                number, code = users.create_invite(user_id, int(argument or "1"))
                await message.answer(f"邀请码编号：{number}\n邀请码仅显示一次，请保存并发给受邀用户：\n/register {code}")
            elif command == "/invites":
                items = users.list_invites(user_id, int(argument or "1"))
                await message.answer("\n".join(f"编号 {i['id']} | 已用 {i['used']}/{i['max_uses']} | {'已撤销' if i['revoked'] else '未撤销'}" for i in items) or "本页暂无邀请码")
            elif command == "/invite_revoke":
                users.require_admin(user_id)
                users.revoke_invite(user_id, int(argument.strip()))
                await message.answer("邀请码已撤销；已注册用户不受影响")
            elif command in {"/ban", "/unban"}:
                users.require_admin(user_id)
                target = int(argument.strip())
                users.set_blocked(user_id, target, command == "/ban")
                await message.answer("已禁用用户并请求取消其未完成任务" if command == "/ban" else "已解除禁用，取消的任务不会自动恢复")
            elif command == "/library":
                await message.answer(settings.komga_public_url or "尚未配置访问地址")
            elif command == "/disk":
                users.require_admin(user_id)
                free = shutil.disk_usage(settings.storage_root).free / 1024**3
                policy = f"暂停阈值 {settings.min_free_space_gb} GiB" if settings.disk_space_check_enabled else "磁盘空间阈值检查已关闭"
                await message.answer(f"剩余 {free:.1f} GiB；{policy}")
            elif command == "/queue":
                page = max(0, int(argument or "1") - 1)
                tasks = service.store.tasks(user_id, page)
                await message.answer(("\n\n".join(describe(t) for t in tasks) or "暂无任务")[:4000])
            elif command == "/status":
                if argument.strip():
                    await message.answer(describe(service.get(argument.strip(), user_id)))
                else:
                    await message.answer(await diagnostics.status(user_id))
            elif command == "/logs":
                content = diagnostics.recent_logs(user_id)
                await message.answer_document(BufferedInputFile(content.encode("utf-8"), filename="jmemby-recent-100.log"),
                                              caption="📜 本次启动以来最近最多 100 条应用日志（已脱敏）", protect_content=True)
            elif command == "/cancel":
                service.store.request_cancel(argument.strip(), user_id)
                await message.answer("已请求取消；停止后会通知，已发布文件保留")
            elif command == "/retry":
                task = service.retry(argument.strip(), user_id)
                sent = await message.answer(describe(task))
                service.store.update(task["id"], message_id=sent.message_id)
            elif command.startswith("/") and command != "/download":
                await message.answer("未知命令，请使用 /help")
            else:
                pending = await message.answer("⏳ 正在获取作品信息，请稍候……")
                manga, token = await service.preview(argument if command == "/download" else text, user_id, message.chat.id)
                keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="确认下载", callback_data=f"confirm:{token}"),
                    InlineKeyboardButton(text="取消", callback_data=f"dismiss:{token}"),
                ]])
                await pending.edit_text(f"{manga.title[:500]}\n作者：{', '.join(manga.authors)[:500]}\n"
                                     f"来源 ID：{manga.source_id}\n章节：{len(manga.chapters)}", reply_markup=keyboard)
        except (ValueError, JobError) as exc:
            if pending:
                await pending.edit_text("⚠️ " + str(exc)[:1000])
            else:
                await message.answer("⚠️ " + str(exc)[:1000])

    @router.callback_query()
    async def callback_handler(callback: CallbackQuery):
        try:
            action, token = (callback.data or "").split(":", 1)
            if action == "dismiss":
                service.store.dismiss(token, callback.from_user.id)
                await callback.answer("已取消")
                await callback.message.delete()
            elif action == "confirm":
                task = service.confirm(token, callback.from_user.id)
                if task["user_id"] != callback.from_user.id:
                    await callback.answer("该漫画已有下载任务")
                    return
                if task["message_id"] and task["message_id"] != callback.message.message_id:
                    await callback.answer("该作品已有任务")
                    await callback.message.delete()
                    return
                if task["status"] in {"COMPLETED", "FAILED", "CANCELLED"}:
                    await callback.answer("任务已结束，请查看结果")
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


async def deliver_notifications(bot: Bot, store, public_url, storage_root=None):
    for notice in store.notifications():
        task = store.get(notice["task_id"])
        completed = task["status"] == "COMPLETED"
        text = completion_text(task) if completed else describe(task)
        try:
            cover = None
            if completed and storage_root:
                chapters = store.chapters(task["id"])
                if chapters:
                    cover = await asyncio.to_thread(read_cover, storage_root, chapters[0]["path"])
            photo = BufferedInputFile(cover, filename="cover.jpg") if cover else None
            edited = False
            if task["message_id"]:
                try:
                    if photo:
                        await bot.edit_message_media(chat_id=task["chat_id"], message_id=task["message_id"],
                                                     media=InputMediaPhoto(media=photo, caption=text))
                    else:
                        await bot.edit_message_text(chat_id=task["chat_id"], message_id=task["message_id"], text=text)
                    edited = True
                except TelegramBadRequest as exc:
                    edited = "message is not modified" in str(exc).lower()
            if not edited:
                if photo:
                    await bot.send_photo(task["chat_id"], photo, caption=text)
                else:
                    await bot.send_message(task["chat_id"], text)
                if task["message_id"]:
                    try:
                        await bot.delete_message(task["chat_id"], task["message_id"])
                    except Exception:
                        pass
            store.notified(notice["id"])
        except TelegramRetryAfter as exc:
            store.defer_notification(notice["id"], exc.retry_after)
        except Exception:
            store.defer_notification(notice["id"], min(3600, 2 ** min(notice["attempts"] + 1, 11)))


async def notifications(bot, store, public_url, storage_root=None):
    last_text = {}
    while True:
        await deliver_notifications(bot, store, public_url, storage_root)
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
            except TelegramBadRequest as exc:
                if "message is not modified" in str(exc).lower():
                    last_text[task["id"]] = text
                else:
                    store.update(task["id"], message_id=None)
            except Exception:
                pass
        await asyncio.sleep(5)
