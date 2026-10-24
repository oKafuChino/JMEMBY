"""Bounded web submission jobs; actual downloads use the durable task queue."""
import asyncio
import logging
import re
import secrets
import time
from copy import deepcopy

from jmemby.domain.errors import JobError
from jmemby.providers.reference import parse_reference

logger = logging.getLogger(__name__)


class BatchDownloads:
    def __init__(self, service):
        self.service = service
        self.batches = {}
        self.jobs = {}
        self.lock = asyncio.Lock()

    def prune(self):
        cutoff = time.monotonic() - 3600
        finished = sorted((b for b in self.batches.values() if b['status'] in {'completed', 'interrupted'}),
                          key=lambda b: b['touched'])
        for batch in finished:
            if batch['touched'] < cutoff or len(self.batches) >= 100:
                self.batches.pop(batch['id'], None)

    def start(self, actor, text):
        self.service.users.require_admin(actor)
        if not isinstance(text, str) or len(text) > 16000:
            raise ValueError('请输入不超过 16000 个字符的漫画 ID 或链接')
        refs = [s for s in re.split(r'[\s,，;；]+', text.strip()) if s]
        if not 1 <= len(refs) <= 50:
            raise ValueError('每批请输入 1–50 项，使用换行、空格或逗号分隔')
        self.prune()
        active = [b for b in self.batches.values() if b['status'] in {'waiting', 'running'}]
        if any(b['user_id'] == actor for b in active):
            raise ValueError('你已有正在提交的批次，请等待完成后再提交')
        if len(active) >= 8:
            raise ValueError('批量提交繁忙，请稍后重试')
        items, seen = [], set()
        for reference in refs:
            item = {'reference': reference, 'source_id': '', 'status': 'pending', 'message': '等待提交',
                    'task_id': '', 'title': ''}
            try:
                source_id = parse_reference(reference, self.service.domains)
                item['source_id'] = source_id
                if source_id in seen:
                    item.update(status='skipped', message='批次内重复 ID，已跳过')
                seen.add(source_id)
            except ValueError as exc:
                item.update(status='invalid', message=str(exc))
            items.append(item)
        batch_id = secrets.token_urlsafe(18)
        batch = {'id': batch_id, 'user_id': actor, 'status': 'waiting', 'items': items,
                 'created_at': time.time(), 'touched': time.monotonic()}
        self.batches[batch_id] = batch
        self.jobs[batch_id] = asyncio.create_task(self.run(batch))
        return self.snapshot(actor, batch_id)

    def latest(self, actor):
        self.service.users.require_admin(actor)
        self.prune()
        owned = [b for b in self.batches.values() if b['user_id'] == actor]
        if not owned:
            return None
        return self.snapshot(actor, max(owned, key=lambda b: b['created_at'])['id'])

    def snapshot(self, actor, batch_id):
        self.service.users.require_admin(actor)
        self.prune()
        batch = self.batches.get(batch_id)
        if not batch or batch['user_id'] != actor:
            raise ValueError('批次不存在或已过期；已入队任务可在下载队列中查看')
        result = deepcopy({k: v for k, v in batch.items() if k != 'touched'})
        result['counts'] = {state: sum(i['status'] == state for i in batch['items'])
                            for state in ('pending', 'submitting', 'queued', 'skipped', 'invalid', 'failed')}
        return result

    async def run(self, batch):
        try:
            async with self.lock:
                batch['status'] = 'running'
                for item in batch['items']:
                    if item['status'] != 'pending':
                        continue
                    item['status'] = 'submitting'
                    item['message'] = '正在获取详情并提交'
                    try:
                        self.service.users.require_admin(batch['user_id'])
                        manga, token = await self.service.preview(item['reference'], batch['user_id'], batch['user_id'])
                        self.service.users.require_admin(batch['user_id'])
                        item['title'] = manga.title[:500]
                        task = self.service.confirm(token, batch['user_id'], batch['user_id'], notify_telegram=False)
                        item.update(status='queued', task_id=task['id'], message='已加入' +
                                    ('白名单优先队列' if task['priority'] else '普通队列'))
                    except ValueError as exc:
                        # Includes duplicate rejection and successful promotion of an
                        # existing task. Show the exact outcome without claiming a new task.
                        item.update(status='skipped', message=str(exc)[:500])
                    except JobError as exc:
                        item.update(status='failed', message=f'[{exc.code}] {str(exc)[:400]}')
                    except Exception as exc:
                        logger.error('batch_submission_failed exception_type=%s', type(exc).__name__)
                        item.update(status='failed', message='提交失败，请检查服务日志后重试')
                batch['status'] = 'completed'
        except asyncio.CancelledError:
            batch['status'] = 'interrupted'
            for item in batch['items']:
                if item['status'] in {'pending', 'submitting'}:
                    item.update(status='failed', message='服务停止，尚未提交；可重新提交并自动去重')
            raise
        finally:
            batch['touched'] = time.monotonic()
            self.jobs.pop(batch['id'], None)

    async def close(self):
        jobs = list(self.jobs.values())
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
