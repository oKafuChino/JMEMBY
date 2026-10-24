"""One ordinary Worker per configured agent, sharing atomic SQLite claims."""
import asyncio
from pathlib import Path
import time

from jmemby.db.store import decode_manga
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

    async def execute(self, task):
        self.current_task = task['id']
        self.coordinator.assign(self.agent, task, decode_manga(task['snapshot']))
        try:
            await super().execute(task)
        finally:
            self.coordinator.unassign(self.agent)
            self.current_task = None

    async def run(self):
        # Recovery runs once in WorkerPool before any node starts claiming work.
        while True:
            self.heartbeat = time.monotonic()
            if not self.coordinator.online(self.agent) or not await self.run_once():
                await asyncio.sleep(1)


class WorkerPool:
    def __init__(self, store, provider, komga, settings, coordinator):
        self.store, self.coordinator = store, coordinator
        self.root = Path(settings.storage_root).resolve()
        active = set()
        self.workers = [AgentWorker(store, provider, komga, settings, coordinator, name, active)
                        for name in coordinator.settings.tokens]

    @property
    def heartbeat(self):
        return min(w.heartbeat for w in self.workers)

    async def run(self):
        self.store.recover()
        # Remove only incomplete uploads from a previous master process.
        for path in (self.root / 'staging').rglob('.upload-*.part'):
            if path.is_file() and not path.is_symlink():
                path.unlink()
        async with asyncio.TaskGroup() as group:
            for worker in self.workers:
                group.create_task(worker.run())
