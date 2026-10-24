"""One ordinary Worker per configured agent, sharing atomic SQLite claims."""
import asyncio
from pathlib import Path
import time

from jmemby.db.store import decode_manga
from jmemby.domain.errors import JobError
from jmemby.distributed.coordinator import DistributedProvider
from jmemby.workers.queue import Worker


class AgentWorker(Worker):
    def __init__(self, store, provider, komga, settings, coordinator, agent, active_tasks):
        self.coordinator, self.agent, self.current_task = coordinator, agent, None
        remote = DistributedProvider(provider, coordinator, agent, self.progress)
        super().__init__(store, remote, komga, settings, active_tasks)

    def progress(self, text):
        if self.current_task:
            self.store.update(self.current_task, progress=text)

    def check(self, task_id):
        super().check(task_id)
        if not self.coordinator.enabled(self.agent) or self.coordinator.revision(self.agent) != self.node_revision:
            raise JobError('AGENT_CHANGED', '节点已停用或安装凭据已更新，任务重新排队', True)

    async def execute(self, task):
        self.current_task = task['id']
        self.node_revision = self.coordinator.revision(self.agent)
        self.coordinator.assign(self.agent, task, decode_manga(task['snapshot']))
        try:
            await super().execute(task)
        finally:
            self.coordinator.unassign(self.agent)
            self.current_task = None

    async def run(self):
        # Recovery runs once in WorkerPool before any node starts claiming work.
        while self.agent in self.coordinator.node_ids():
            self.heartbeat = time.monotonic()
            if not self.coordinator.online(self.agent) or not await self.run_once():
                await asyncio.sleep(1)


class WorkerPool:
    def __init__(self, store, provider, komga, settings, coordinator):
        self.store, self.coordinator = store, coordinator
        self.provider, self.komga, self.settings = provider, komga, settings
        self.root = Path(settings.storage_root).resolve()
        self.active_tasks = set()
        self.workers = [self.new_worker(name) for name in coordinator.node_ids()]

    def new_worker(self, name):
        return AgentWorker(self.store, self.provider, self.komga, self.settings,
                           self.coordinator, name, self.active_tasks)

    @property
    def heartbeat(self):
        return min((w.heartbeat for w in self.workers), default=time.monotonic())

    async def run(self):
        self.store.recover()
        # Remove only incomplete uploads from a previous master process.
        for path in (self.root / 'staging').rglob('.upload-*.part'):
            if path.is_file() and not path.is_symlink():
                path.unlink()
        async with asyncio.TaskGroup() as group:
            jobs = {w.agent: group.create_task(w.run()) for w in self.workers}
            while True:
                names = set(self.coordinator.node_ids())
                for worker in list(self.workers):
                    job = jobs[worker.agent]
                    if job.done() or (worker.agent not in names and worker.current_task is None):
                        job.cancel()
                        jobs.pop(worker.agent)
                        self.workers.remove(worker)
                for name in sorted(names - jobs.keys()):
                    worker = self.new_worker(name)
                    self.workers.append(worker)
                    jobs[name] = group.create_task(worker.run())
                await asyncio.sleep(1)
