"""A fast durable clock tick and one bounded discovery operation at a time."""
import asyncio
import json
import logging
import time

from jmemby.domain.errors import JobError
from jmemby.domain.schedule_tags import schedule_tags
from jmemby.services.discovery import DiscoveryService

logger = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, service):
        self.service = service
        self.heartbeat = time.monotonic()

    async def process(self, run):
        s = self.service
        config = json.loads(run["snapshot"])

        def check():
            current = s.db.execute("SELECT status FROM schedule_runs WHERE id=?", (run["id"],)).fetchone()
            if not current or current[0] not in {"DISCOVERING", "PLANNED"} or not s.valid_run(run):
                raise JobError("SCHEDULE_CANCELLED", "计划已暂停或权限变化")

        try:
            check()
            remaining = min(600, run["created_at"] + 600 - s.clock())
            if remaining <= 0:
                raise JobError("DISCOVERY_BUDGET", "运行预算已用尽")
            if s.pending_count() >= 50:
                raise JobError("BACKPRESSURE", "自动下载队列已达到 50 部上限", True, 15)
            async with asyncio.timeout(remaining):
                if run["status"] == "DISCOVERING":
                    async with s.discovery_lock:
                        result = await DiscoveryService(s.provider, s.skip_async, s.clock).collect(
                            schedule_tags(config), config["item_limit"], check)
                    check()
                    s.save_plan(run, result)
                check()
                skips = await s.prepare_admission(run)
                check()
                s.admit(run, skips)
        except (JobError, TimeoutError) as exc:
            current = s.db.execute("SELECT status FROM schedule_runs WHERE id=?", (run["id"],)).fetchone()
            if not current or current[0] not in {"DISCOVERING", "PLANNED"}:
                return
            attempts = run["attempts"] + 1
            delay = max(getattr(exc, "delay", 0), 2 ** attempts)
            if (getattr(exc, "code", "") == "BACKPRESSURE"
                    and s.clock() + 15 < run["created_at"] + 600):
                s.db.execute("UPDATE schedule_runs SET next_attempt_at=?,error='BACKPRESSURE' WHERE id=?", (s.clock() + 15, run["id"]))
            elif (getattr(exc, "retryable", False) and attempts < 3
                    and s.clock() + delay < run["created_at"] + 600):
                s.db.execute("UPDATE schedule_runs SET attempts=?,next_attempt_at=?,error=? WHERE id=?",
                             (attempts, s.clock() + delay, getattr(exc, "code", "TIMEOUT"), run["id"]))
            else:
                code = getattr(exc, "code", "DISCOVERY_TIMEOUT")
                with s.store.transaction():
                    if code in {"DISCOVERY_UNSUPPORTED", "JM_SDK_INCOMPATIBLE"}:
                        s.db.execute("UPDATE schedules SET enabled=0,revision=revision+1 WHERE id=?", (run["schedule_id"],))
                    s.end_run(run["id"], "CANCELLED" if code == "SCHEDULE_CANCELLED" else "FAILED", code)
        except Exception as exc:
            # Do not swallow storage/corruption errors and keep a false heartbeat.
            logger.error("schedule_failure run=%s type=%s", run["id"], type(exc).__name__)
            raise

    async def run(self):
        operation = None
        try:
            while True:
                self.heartbeat = time.monotonic()
                self.service.reconcile()
                self.service.claim_due()
                if operation and operation.done():
                    operation.result()
                    operation = None
                if operation is None:
                    row = self.service.db.execute("SELECT * FROM schedule_runs WHERE status IN ('DISCOVERING','PLANNED') AND next_attempt_at<=? ORDER BY created_at LIMIT 1", (self.service.clock(),)).fetchone()
                    if row:
                        operation = asyncio.create_task(self.process(dict(row)))
                await asyncio.sleep(1)
        finally:
            if operation:
                operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)
