"""One VPS owns one whole manga until completion, failure or cancellation.

Chapters are transported individually but stay pinned to the manga's agent.
SQLite retains the durable queue; temporary leases die on master restart.
"""
import asyncio
from dataclasses import dataclass, field
import logging
from pathlib import Path
import re
import secrets
import time

from jmemby.domain.errors import JobError

logger = logging.getLogger(__name__)


class LeaseConflict(Exception):
    pass


@dataclass
class Download:
    id: str
    agent: str
    book: dict
    pages: dict
    stage: Path
    max_bytes: int
    max_pixels: int
    budget: int
    deadline: float
    received: int = 0
    completed: set = field(default_factory=set)
    lease: str = ""
    expires: float = 0
    done: bool = False
    error: JobError | None = None


class Coordinator:
    PROBE_TIMEOUT = 25

    def __init__(self, settings, clock=time.monotonic):
        self.settings, self.clock = settings, clock
        self.jobs, self.agents, self.books, self.instances = {}, {}, {}, {}
        self.uploads = set()
        self.registry = None
        self.probes, self.probe_results = {}, {}

    def node_ids(self):
        return self.registry.ids() if self.registry else list(self.settings.tokens)

    def enabled(self, agent):
        return self.registry.enabled(agent) if self.registry else agent in self.settings.tokens

    def revision(self, agent):
        if not self.registry:
            return 0
        try:
            return self.registry.get(agent)['revision']
        except ValueError:
            return -1

    def authenticate(self, agent, token):
        if self.registry:
            return self.registry.authenticate(agent, token)
        expected = self.settings.tokens.get(agent)
        return bool(expected and secrets.compare_digest(token.encode(), expected.encode()))

    def invalidate(self, agent):
        self.agents.pop(agent, None)
        self.instances.pop(agent, None)
        self.probe_results.pop(agent, None)
        probe = self.probes.get(agent)
        if probe and not probe['future'].done():
            probe['future'].set_result('changed')
        for job in self.jobs.values():
            if job.agent == agent:
                job.error = JobError('AGENT_CHANGED', '节点配置已变更，任务重新排队', True)

    async def test_connection(self, agent):
        if agent not in self.node_ids():
            raise ValueError('节点不存在')
        if not self.enabled(agent):
            raise ValueError('请先启用节点，再测试连通性')
        if self.registry and not self.registry.public(agent)['registered']:
            raise ValueError('节点尚未安装，请先执行安装命令')
        if agent in self.probes:
            raise ValueError('该节点正在测试，请稍候')
        started = self.clock()
        probe = {'nonce': secrets.token_urlsafe(32), 'future': asyncio.get_running_loop().create_future(),
                 'started': started, 'revision': self.revision(agent), 'instance': None}
        self.probes[agent] = probe
        try:
            try:
                outcome = await asyncio.wait_for(probe['future'], self.PROBE_TIMEOUT)
            except TimeoutError:
                outcome = 'timeout'
            if self.revision(agent) != probe['revision']:
                outcome = 'changed'
            result = {'status': outcome, 'checked_at': time.time(),
                      'elapsed_ms': round(max(0, self.clock() - started) * 1000)}
            if outcome != 'changed' and agent in self.node_ids():
                self.probe_results[agent] = result
            return result
        finally:
            if self.probes.get(agent) is probe:
                self.probes.pop(agent)

    def probe_offer(self, agent):
        probe = self.probes.get(agent)
        if not probe or probe['future'].done() or not self.enabled(agent):
            return None
        if self.clock() - probe['started'] >= self.PROBE_TIMEOUT:
            return None
        instance = self.instances.get(agent)
        if not instance:
            return None
        if probe['instance'] is None:
            probe['instance'] = instance
        if probe['instance'] != instance:
            return None
        return probe['nonce']

    def probe_ack(self, agent, nonce):
        probe = self.probes.get(agent)
        if (not probe or probe['future'].done() or not self.enabled(agent)
                or self.revision(agent) != probe['revision'] or not probe['instance']
                or self.instances.get(agent) != probe['instance']
                or self.clock() - probe['started'] >= self.PROBE_TIMEOUT
                or not isinstance(nonce, str) or not secrets.compare_digest(probe['nonce'], nonce)):
            raise LeaseConflict('Invalid or expired connectivity probe')
        self.touch(agent)
        probe['future'].set_result('success')

    def connect(self, agent, instance):
        if not re.fullmatch(r'[0-9a-f]{32}', instance):
            raise ValueError('Agent instance ID required')
        previous = self.instances.get(agent)
        if previous and previous != instance and self.online(agent):
            raise LeaseConflict('Another process is using this agent ID')
        self.instances[agent] = instance

    def touch(self, agent):
        self.agents[agent] = self.clock()

    def online(self, agent):
        return self.enabled(agent) and self.clock() - self.agents.get(agent, -1e20) < max(30, self.settings.lease_seconds)

    def snapshot(self):
        return {"enabled": True, "agents": [
            {**(self.registry.public(name) if self.registry else {'id': name, 'label': name, 'enabled': True, 'concurrency': 2}),
             "online": self.online(name), "task": self.books.get(name),
             "connectivity_test": self.probe_results.get(name), "testing": name in self.probes,
             "last_seen_seconds": round(max(0, self.clock() - self.agents[name])) if name in self.agents else None,
             "received_pages": sum(len(j.completed) for j in self.jobs.values() if j.agent == name),
             "total_pages": sum(len(j.pages) for j in self.jobs.values() if j.agent == name)}
            for name in self.node_ids()]}

    def assign(self, agent, task, manga):
        if agent in self.books:
            raise RuntimeError('Agent already owns a manga')
        self.books[agent] = {"task_id": task['id'], "source_id": manga.source_id, "title": manga.title}
        logger.info('agent_book_assigned agent=%s task=%s', agent, task['id'])

    def unassign(self, agent):
        self.books.pop(agent, None)
        for job_id in [key for key, job in self.jobs.items() if job.agent == agent]:
            self.jobs.pop(job_id, None)

    def claim(self, agent):
        if not self.enabled(agent):
            raise LeaseConflict('Node disabled')
        self.touch(agent)
        job = next((j for j in self.jobs.values() if j.agent == agent and not j.done and not j.error), None)
        if job is None:
            return None
        if job.deadline <= self.clock() or (job.lease and job.expires <= self.clock()):
            job.error = JobError('AGENT_OFFLINE', '下载节点租约过期，漫画将按原队列规则重试', True)
            return None
        if not job.lease:
            job.lease = secrets.token_urlsafe(32)
            job.expires = self.clock() + self.settings.lease_seconds
        return {"id": job.id, "lease": job.lease, "lease_seconds": self.settings.lease_seconds,
                "concurrency": self.registry.get(agent)['concurrency'] if self.registry else None,
                "book": job.book,
                "pages": [{"number": p.number, "url": p.url} for n, p in job.pages.items()
                          if n not in job.completed],
                "max_bytes": job.max_bytes, "max_pixels": job.max_pixels,
                "budget": max(0, job.budget - job.received)}

    def owned(self, job_id, agent, lease):
        job = self.jobs.get(job_id)
        if (job is None or not lease or job.agent != agent or not self.enabled(agent)
                or not secrets.compare_digest(job.lease, lease) or job.expires <= self.clock()
                or job.deadline <= self.clock() or job.error):
            raise LeaseConflict('Lease expired, cancelled or reassigned')
        self.touch(agent)
        return job

    def heartbeat(self, job_id, agent, lease):
        job = self.owned(job_id, agent, lease)
        job.expires = self.clock() + self.settings.lease_seconds

    def finish(self, job_id, agent, lease):
        job = self.owned(job_id, agent, lease)
        if set(job.pages) != job.completed:
            raise LeaseConflict('Some pages have not been uploaded')
        job.done = True

    def fail(self, job_id, agent, lease, code=None):
        job = self.owned(job_id, agent, lease)
        if not job.done:
            reasons = {
                'MEMORY_LIMIT': '节点内存不足，请使用低内存模式或内存更充足的节点',
                'PROVIDER_KILLED': '节点下载进程被强制结束，请检查 VPS 内存限制和 OOM 日志',
                'IMAGE_LIMIT': '图片超过节点的解码尺寸限制，请改用内存更充足的节点',
                'DISK_LOW': '节点临时磁盘空间不足，请清理空间或检查保留空间配置',
                'DISK_FULL': '节点磁盘已满，请先释放空间',
            }
            # Do not reflect arbitrary remote strings or credentials to users/logs.
            reason = reasons.get(code) if isinstance(code, str) else None
            job.error = JobError('AGENT_RESOURCE_LIMIT' if reason else 'AGENT_FAILED',
                                 reason or '远程下载失败，请检查对应 VPS Agent 日志', True)

    def accept(self, job_id, agent, lease, number, temporary, extension):
        # No awaits between final ownership check, budget accounting and publication.
        job = self.owned(job_id, agent, lease)
        if number not in job.pages:
            raise LeaseConflict('Page does not belong to this chapter')
        if number in job.completed:
            return
        size = temporary.stat().st_size
        if job.received + size > job.budget:
            job.error = JobError('SIZE_LIMIT', '远程下载任务体积超出限制')
            raise job.error
        temporary.replace(job.stage / f'{number:06d}{extension}')
        job.received += size
        job.completed.add(number)

    async def download_batch(self, agent, pages, stage, max_bytes, max_pixels, budget, progress=None):
        if not pages:
            return
        if (agent not in self.books or len({p.number for p in pages}) != len(pages) or budget < 0
                or len(pages) > 10000
                or any(type(p.number) is not int or p.number < 1
                       or not re.fullmatch(r'jm:[0-9]+:[0-9]+', p.url) for p in pages)):
            raise JobError('AGENT_PROTOCOL', '远程下载页清单或漫画分配无效')
        if any(j.agent == agent for j in self.jobs.values()):
            raise RuntimeError('Agent already has an active chapter')
        job = Download(secrets.token_hex(16), agent, dict(self.books[agent]),
                       {p.number: p for p in pages}, Path(stage), max_bytes, max_pixels,
                       budget, self.clock() + self.settings.job_timeout)
        self.jobs[job.id] = job
        previous = -1
        try:
            while not job.done:
                if job.error:
                    raise job.error
                if self.clock() >= job.deadline:
                    raise JobError('AGENT_TIMEOUT', '等待远程章节下载超时，请检查对应 Agent', True)
                if (job.lease and job.expires <= self.clock()) or (not job.lease and not self.online(agent)):
                    raise JobError('AGENT_OFFLINE', '下载节点失联，漫画将重新排队', True)
                if progress and previous != len(job.completed):
                    previous = len(job.completed)
                    progress(f'节点 {agent}：当前章节已接收 {previous}/{len(job.pages)} 页')
                await asyncio.sleep(0.2)
        finally:
            self.jobs.pop(job.id, None)


class DistributedProvider:
    def __init__(self, provider, coordinator, agent, progress):
        self.provider, self.coordinator, self.agent, self.progress = provider, coordinator, agent, progress
        self.source = provider.source

    async def get_manga(self, source_id):
        return await self.provider.get_manga(source_id)

    async def get_pages(self, chapter_id):
        return await self.provider.get_pages(chapter_id)

    async def download_batch(self, pages, stage, max_bytes, max_pixels, budget):
        await self.coordinator.download_batch(self.agent, pages, stage, max_bytes, max_pixels, budget, self.progress)

    async def download(self, page, destination, max_bytes):
        raise JobError('AGENT_INCOMPLETE', '远程返回的图片缺失或无效', True)
