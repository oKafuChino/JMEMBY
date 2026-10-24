"""Durable channel announcements for completed imports, independent of user replies."""
import asyncio
import json
import logging
import math
import time

from jmemby.services.membership import MembershipService


logger = logging.getLogger(__name__)
KEY = 'channel_push'


def announcement_text(task, public_url):
    manga = json.loads(task['snapshot'])
    authors = ', '.join(manga.get('authors', []))[:150] or '未知'
    text = (f"📚 新漫画已入库\n标题：{str(manga['title'])[:300]}\n作者：{authors}\n"
            f"漫画 ID：{str(manga['source_id'])[:50]}\n章节：{len(manga['chapters'])}\n"
            f"入库编号：{task['id']}")
    if public_url:
        text += '\n阅读地址：' + public_url[:1000]
    return text


class ChannelPushService:
    def __init__(self, store, users, bot=None, public_url=''):
        self.store, self.users, self.bot, self.public_url = store, users, bot, public_url
        self.save_lock, self.delivery_lock = asyncio.Lock(), asyncio.Lock()

    def config(self):
        row = self.store.connection.execute('SELECT value FROM settings WHERE key=?', (KEY,)).fetchone()
        return json.loads(row['value']) if row else {'enabled': False, 'channel': None, 'include_batch': False}

    def settings(self, actor):
        self.users.require_admin(actor)
        counts = {r['status']: r['n'] for r in self.store.connection.execute(
            'SELECT status,COUNT(*) n FROM channel_deliveries GROUP BY status')}
        recent = [dict(r) for r in self.store.connection.execute(
            'SELECT d.*,m.title,m.source_id FROM channel_deliveries d JOIN tasks t ON t.id=d.task_id '
            'JOIN manga m ON m.id=t.manga_id ORDER BY d.id DESC LIMIT 10')]
        return {**self.config(), 'counts': counts, 'recent': recent}

    async def save(self, actor, data):
        self.users.require_admin(actor)
        if not isinstance(data, dict) or type(data.get('enabled')) is not bool:
            raise ValueError('请提供有效的推送开关')
        async with self.save_lock:
            if not data['enabled']:
                config = {**self.config(), 'enabled': False}
            else:
                include_batch = data.get('include_batch', False)
                if type(include_batch) is not bool:
                    raise ValueError('批量推送开关无效')
                chat_id, _ = MembershipService.target_input({'chat_id': data.get('chat_id')}, '推送频道')
                if self.bot is None:
                    raise ValueError('Bot 连接不可用，无法验证推送频道')
                try:
                    async with asyncio.timeout(15):
                        chat = await self.bot.get_chat(chat_id)
                        if chat.type != 'channel':
                            raise ValueError('推送目标必须是频道，不能是群组或私聊')
                        own = await self.bot.get_chat_member(chat.id, self.bot.id)
                        if not (own.status == 'creator' or
                                (own.status == 'administrator' and getattr(own, 'can_post_messages', False))):
                            raise ValueError('请将 Bot 设为该频道管理员，并授予发布消息权限')
                except ValueError:
                    raise
                except Exception as exc:
                    logger.warning('channel_config_failed type=%s', type(exc).__name__)
                    raise ValueError('无法验证频道，请检查 Chat ID、Bot 权限和网络；原配置保持不变') from None
                config = {'enabled': True, 'include_batch': include_batch,
                          'channel': {'chat_id': chat.id, 'title': (chat.title or '频道')[:128]}}
            self.users.require_admin(actor)
            with self.store.transaction() as db:
                db.execute('INSERT INTO settings(key,value) VALUES (?,?) '
                           'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (KEY, json.dumps(config, ensure_ascii=False)))
                if not config['enabled']:
                    db.execute("UPDATE channel_deliveries SET status='SKIPPED',last_error='DISABLED' WHERE status='PENDING'")
                else:
                    db.execute("UPDATE channel_deliveries SET status='SKIPPED',last_error='CHANNEL_CHANGED' "
                               "WHERE status='PENDING' AND chat_id<>?", (config['channel']['chat_id'],))
                    if not config['include_batch']:
                        db.execute("UPDATE channel_deliveries SET status='SKIPPED',last_error='BATCH_SILENT' "
                                   "WHERE status='PENDING' AND task_id IN (SELECT id FROM tasks WHERE notify_telegram=0)")
            return self.settings(actor)

    async def deliver_once(self):
        # Only one sender in this process; the existing master instance lock covers processes.
        async with self.delivery_lock:
            config = self.config()
            if not config['enabled'] or self.bot is None:
                return
            notice = self.store.connection.execute(
                "SELECT d.*,t.snapshot,t.status task_status,t.notify_telegram,m.removed_at "
                "FROM channel_deliveries d JOIN tasks t ON t.id=d.task_id JOIN manga m ON m.id=t.manga_id "
                "WHERE d.status='PENDING' ORDER BY d.id LIMIT 1").fetchone()
            if not notice:
                return
            chapters = self.store.chapters(notice['task_id'])
            if (notice['chat_id'] != config['channel']['chat_id'] or notice['task_status'] != 'COMPLETED'
                    or (not notice['notify_telegram'] and not config['include_batch'])
                    or (notice['removed_at'] is not None and notice['removed_at'] >= notice['created_at'])
                    or not chapters or any(c['status'] != 'IMPORTED' for c in chapters)):
                self.store.connection.execute("UPDATE channel_deliveries SET status='SKIPPED',last_error='NO_LONGER_ELIGIBLE' WHERE id=?", (notice['id'],))
                return
            # Keep the head's backoff for the channel so a 429 does not trigger every queued row.
            if notice['next_at'] > time.time():
                return
            task = {'id': notice['task_id'], 'snapshot': notice['snapshot']}
            try:
                async with asyncio.timeout(15):
                    result = await self.bot.send_message(notice['chat_id'], announcement_text(task, self.public_url),
                                                         parse_mode=None, link_preview_options={'is_disabled': True})
                self.store.connection.execute(
                    "UPDATE channel_deliveries SET status='SENT',sent_at=?,message_id=?,last_error=NULL WHERE id=?",
                    (time.time(), result.message_id, notice['id']))
            except Exception as exc:
                retry = getattr(exc, 'retry_after', None)
                delay = retry if type(retry) in (int, float) and math.isfinite(retry) and retry > 0 else min(3600, 2 ** min(notice['attempts'] + 1, 12))
                self.store.connection.execute(
                    "UPDATE channel_deliveries SET attempts=attempts+1,next_at=?,last_error=? WHERE id=? AND status='PENDING'",
                    (time.time() + delay, type(exc).__name__[:80], notice['id']))
                logger.warning('channel_delivery_failed id=%s type=%s', notice['id'], type(exc).__name__)

    async def run(self):
        while True:
            await self.deliver_once()
            await asyncio.sleep(5)
