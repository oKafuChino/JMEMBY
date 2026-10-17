"""Admin-only schedule router, registered before the legacy catch-all handler."""
import asyncio
from collections import Counter
from datetime import datetime
import json
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.exceptions import TelegramRetryAfter

from jmemby.domain.errors import JobError
from jmemby.domain.schedule_tags import parse_tags, schedule_tags
from jmemby.services.discovery import DiscoveryService
from jmemby.bot.presentation import normalize
from jmemby.services.schedule_display import reason_label, SORT_LABELS

COMMANDS = {"/schedules", "/schedule_add", "/schedule_cancel", "/schedule_preview",
            "/schedule_enable", "/schedule_pause", "/schedule_run", "/schedule_runs",
            "/schedule_cancel_run"}


def describe_schedule(row):
    spec = json.loads(row["spec"])
    next_at = row["next_run_at"]
    local = datetime.fromtimestamp(next_at, ZoneInfo(spec["timezone"])).isoformat() if next_at else "无"
    kind = {"once": "单次", "daily": "每日", "weekly": "每周"}[spec["kind"]]
    weekday = f"星期{spec['weekday']}" if spec["kind"] == "weekly" else ""
    tags = schedule_tags(row)
    label = "标签（匹配任一）" if len(tags) > 1 else "标签"
    return (f"计划 #{row['id']} | {'启用' if row['enabled'] else '暂停'}\n{label}：{'、'.join(tags)}\n"
            f"{kind} {spec.get('date', '')} {weekday} {spec['time']} {spec['timezone']}\n"
            f"每次最多 {row['item_limit']} 部；AI 标记强制排除\n下一次：{local}")


def create_schedule_router(service, settings):
    router = Router(name="schedules")

    async def matches(message):
        # aiogram dispatches synchronous filters to a thread pool. SQLite must
        # remain on the event-loop thread that owns this connection.
        text = normalize(message.text or "")
        command = text.split(maxsplit=1)[0].split("@")[0] if text else ""
        if command in COMMANDS:
            return True
        if not text or text.startswith("/") or not message.from_user:
            return False
        return service.db.execute("SELECT 1 FROM schedule_drafts WHERE owner_user_id=?",
                                  (message.from_user.id,)).fetchone() is not None

    def authorize(event):
        user = event.from_user
        message = getattr(event, "message", None) or event
        chat = getattr(message, "chat", None)
        if not user or not chat or chat.type != "private" or chat.id != user.id:
            raise ValueError("计划管理仅支持管理员私聊")
        service.users.require_admin(user.id)
        return user.id

    async def confirm(message, schedule_id, actor, action):
        row = service.owned(schedule_id, actor)
        if action == "enable":
            from jmemby.services.schedule_time import next_time
            row["next_run_at"] = next_time(json.loads(row["spec"]), service.clock())
        token = service.action(schedule_id, actor, action)
        protection = "已开启" if settings.disk_space_check_enabled else "已关闭，请预留存储空间"
        action_label = "启用计划" if action == "enable" else "立即执行一次" if action == "run" else "取消本批次"
        await message.answer(describe_schedule(row) + f"\n磁盘阈值保护：{protection}\n确认{action_label}？",
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                                 InlineKeyboardButton(text="确认", callback_data="sched:" + token)]]))

    async def wizard(message, actor, text):
        row = service.db.execute("SELECT * FROM schedule_drafts WHERE owner_user_id=?", (actor,)).fetchone()
        if not row or row["expires"] <= service.clock():
            service.db.execute("DELETE FROM schedule_drafts WHERE owner_user_id=?", (actor,))
            await message.answer("创建会话已过期，请发送 /schedule_add 重新开始。")
            return
        data = json.loads(row["data"])
        step = row["step"]
        if step == "tag":
            data["tags"] = parse_tags(text)
            step, prompt = "kind", (f"已接收标签：{'、'.join(data['tags'])}（匹配任一标签）。\n"
                                    "输入 once（单次）、daily（每日）或 weekly（每周）。")
        elif step == "kind":
            if text not in {"once", "daily", "weekly"}:
                raise ValueError("请输入 once、daily 或 weekly")
            data["kind"] = text
            step, prompt = "when", {"once": "输入日期和时间，例如 2026-12-01 03:00。",
                                    "daily": "输入时间，例如 03:00。",
                                    "weekly": "输入星期和时间，例如 1 03:00（1=周一，7=周日）。"}[text]
        elif step == "when":
            parts = text.split()
            if data["kind"] == "daily" and len(parts) == 1:
                data["time"] = parts[0]
            elif data["kind"] == "once" and len(parts) == 2:
                data["date"], data["time"] = parts
            elif data["kind"] == "weekly" and len(parts) == 2:
                data["weekday"], data["time"] = int(parts[0]), parts[1]
            else:
                raise ValueError("时间格式不正确，请按提示重试")
            step, prompt = "zone", f"输入 IANA 时区，例如 Asia/Hong_Kong；输入 default 使用 {settings.schedule_default_timezone}。"
        elif step == "zone":
            data["timezone"] = settings.schedule_default_timezone if text == "default" else text
            from jmemby.services.schedule_time import next_time
            if next_time(data, service.clock()) is None:
                raise ValueError("时间已过期或不存在，请 /schedule_add 重新创建")
            step, prompt = "limit", "输入每次最多新入队部数（1–20，建议 5）。"
        else:
            spec = {k: v for k, v in data.items() if k not in {"tag", "tags"}}
            tags = data["tags"] if "tags" in data else [data["tag"]]
            schedule = service.create(actor, tags, spec, int(text))
            service.db.execute("DELETE FROM schedule_drafts WHERE owner_user_id=?", (actor,))
            await message.answer(describe_schedule(schedule) + f"\n已保存为暂停。先 /schedule_preview {schedule['id']}，再 /schedule_enable {schedule['id']}。")
            return
        service.db.execute("UPDATE schedule_drafts SET step=?,data=?,expires=? WHERE owner_user_id=?", (step, json.dumps(data), service.clock() + 600, actor))
        await message.answer(prompt + "\n/schedule_cancel 退出创建。")

    @router.message(matches)
    async def message_handler(message):
        try:
            actor = authorize(message)
            head, _, arg = normalize(message.text).partition(" ")
            command = head.split("@")[0]
            if command == "/schedule_add":
                service.db.execute("INSERT OR REPLACE INTO schedule_drafts VALUES (?,?,?,?)", (actor, "tag", "{}", service.clock() + 600))
                await message.answer("请输入 1–10 个漫画标签，用逗号、顿号或换行分隔，例如：冒险，科幻。\n"
                                     "匹配任一标签即可，作品去重后共用每次数量；标签内空格保留。\n"
                                     "多标签按精确点赞数合并排序，无法确认点赞数的作品跳过。\n/schedule_cancel 退出创建。")
            elif command == "/schedule_cancel":
                service.db.execute("DELETE FROM schedule_drafts WHERE owner_user_id=?", (actor,))
                await message.answer("已退出计划创建")
            elif command == "/schedules":
                rows = service.list(actor, int(arg or "1"))
                await message.answer(("\n\n".join(describe_schedule(r) for r in rows) or "暂无计划")[:3500]
                                     + "\n/schedule_add 创建\n/schedule_preview ID 预览\n/schedule_enable ID 启用\n/schedule_pause ID 暂停\n/schedule_run ID 立即运行\n/schedule_runs ID 历史\n/schedule_cancel_run 批次ID 取消批次")
            elif command == "/schedule_preview":
                row = service.owned(int(arg), actor)
                await message.answer("正在按点赞优先搜索并排除 AI 标记；最多等待 10 分钟，不创建下载任务。")

                def check():
                    current = service.owned(row["id"], actor)
                    if current["revision"] != row["revision"]:
                        raise ValueError("预览期间计划发生变化，请重新预览")

                async with asyncio.timeout(600):
                    async with service.discovery_lock:
                        result = await DiscoveryService(service.provider, service.skip_async, service.clock).collect(schedule_tags(row), row["item_limit"], check)
                check()
                selected = [i for i in result["items"] if i["decision"] == "ALLOW"][:row["item_limit"]]
                counts = Counter(reason_label(i["reason"]) for i in result["items"] if i["decision"] != "ALLOW")
                lines = [f"{i['source_id']} | {i['snapshot']['title'][:80]} | 点赞 {i['likes_raw'] or '未知'}" for i in selected]
                scope = "全部时间" if result["scope"] == "all" else result["scope"]
                excluded = "；".join(f"{key} {count} 部" for key, count in counts.items()) or "无"
                await message.answer((f"排序：{SORT_LABELS[result['mode']]}；范围：{scope}"
                                      + ("；达到候选预算" if result["truncated"] else "") + "\n"
                                      + "\n".join(lines) + f"\n预计 {len(selected)} 部；排除：{excluded}\n正式运行会重新搜索和检查。")[:3900])
            elif command in {"/schedule_enable", "/schedule_run"}:
                await confirm(message, int(arg), actor, "enable" if command.endswith("enable") else "run")
            elif command == "/schedule_pause":
                service.pause(int(arg), actor)
                await message.answer("已暂停未来执行及尚未入队的批次；已入队任务继续。")
            elif command == "/schedule_runs":
                runs = service.history(int(arg), actor)
                await message.answer(("\n\n".join(service.summary(r["id"]) for r in runs[:3]) or "暂无运行")[:3900])
            elif command == "/schedule_cancel_run":
                run = service.db.execute("SELECT schedule_id FROM schedule_runs WHERE id=?", (arg.strip(),)).fetchone()
                if not run:
                    raise ValueError("批次不存在")
                await confirm(message, run[0], actor, "cancel:" + arg.strip())
            else:
                await wizard(message, actor, message.text.strip())
        except TimeoutError:
            await message.answer("搜索预算耗尽，未创建下载任务。")
        except (ValueError, JobError) as exc:
            await message.answer(str(exc)[:1000])

    @router.callback_query(F.data.startswith("sched:"))
    async def callback_handler(callback):
        try:
            actor = authorize(callback)
            result = service.confirm_action(callback.data[6:], actor)
            await callback.answer("已处理")
            text = f"已创建运行批次：{result}\n选书及下载结果会另行通知。" if len(result) == 32 and all(c in "0123456789abcdef" for c in result) else result
            await callback.message.edit_text(text)
        except (ValueError, JobError) as exc:
            await callback.answer(str(exc)[:180], show_alert=True)

    return router


async def schedule_notifications(bot, service):
    while True:
        rows = service.db.execute("SELECT n.*,s.chat_id,s.owner_user_id FROM schedule_notifications n JOIN schedule_runs r ON r.id=n.run_id JOIN schedules s ON s.id=r.schedule_id WHERE n.status='PENDING' AND n.next_at<=? LIMIT 10", (service.clock(),)).fetchall()
        for n in rows:
            if not service.users.is_admin(n["owner_user_id"]):
                service.db.execute("UPDATE schedule_notifications SET status='SKIPPED' WHERE id=?", (n["id"],))
                continue
            try:
                await bot.send_message(n["chat_id"], n["text"])
                service.db.execute("UPDATE schedule_notifications SET status='SENT' WHERE id=?", (n["id"],))
            except Exception as exc:
                delay = exc.retry_after if isinstance(exc, TelegramRetryAfter) else min(3600, 2 ** min(n["attempts"] + 1, 11))
                service.db.execute("UPDATE schedule_notifications SET attempts=attempts+1,next_at=? WHERE id=?", (service.clock() + delay, n["id"]))
        await asyncio.sleep(5)
