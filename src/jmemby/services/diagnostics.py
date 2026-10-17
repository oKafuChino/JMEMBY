"""Admin-only process diagnostics; no Docker socket or shell access required."""
import asyncio
from collections import deque
import logging
import os
import re
import shutil
import time

from jmemby.domain.errors import JobError


class RecentLogs(logging.Handler):
    def __init__(self):
        super().__init__(logging.INFO)
        self.lines = deque(maxlen=100)
        self.secrets = tuple(v for k, v in os.environ.items() if v and
                             any(s in k.upper() for s in ("TOKEN", "PASSWORD", "SECRET", "COOKIE", "API_KEY")))
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))

    def emit(self, record):
        try:
            text = self.format(record)
            for secret in sorted(self.secrets, key=len, reverse=True):
                text = text.replace(secret, "[REDACTED]")
            text = re.sub(r"\b\d{5,}:[A-Za-z0-9_-]{20,}\b", "[REDACTED]", text)
            text = re.sub(r"https?://\S+", "[URL REDACTED]", text)
            text = re.sub(r"(?i)(password|token|secret|cookie|authorization)([\"']?\s*[:=]\s*)([^\s,}]+)",
                          r"\1\2[REDACTED]", text)
            self.lines.append(text.replace("\r", "\\r").replace("\n", "\\n")[:4000])
        except Exception:
            self.handleError(record)

    def snapshot(self):
        self.acquire()
        try:
            return "\n".join(self.lines) or "暂无日志"
        finally:
            self.release()


class Diagnostics:
    def __init__(self, users, worker, komga, logs):
        self.users, self.worker, self.komga, self.logs = users, worker, komga, logs
        self.started = time.monotonic()

    def recent_logs(self, actor):
        self.users.require_admin(actor)
        return self.logs.snapshot()

    async def status(self, actor):
        self.users.require_admin(actor)
        rows = self.users.store.connection.execute("SELECT status, COUNT(*) FROM tasks GROUP BY status").fetchall()
        stats = self.users.statistics(actor)
        age = max(0, time.monotonic() - self.worker.heartbeat)
        try:
            disk = shutil.disk_usage(self.worker.root)
            disk_text = f"可用 {disk.free / 1024**3:.1f} / 总计 {disk.total / 1024**3:.1f} GiB"
        except OSError:
            disk_text = "无法读取"
        try:
            await asyncio.wait_for(self.komga.request({"operation": "check"}), timeout=10)
            komga = "连接及漫画库检查通过"
        except JobError as exc:
            komga = f"检查失败 [{exc.code}]"
        except TimeoutError:
            komga = "检查超时"
        except Exception:
            komga = "检查失败，请查看应用日志"
        return (f"🩺 服务状态\n\n运行时长：{int(time.monotonic() - self.started)} 秒\n"
                f"Worker：{'正常' if age < 30 else '心跳延迟'}（{age:.0f} 秒前）\n"
                f"数据库：可查询\n磁盘：{disk_text}\nKomga：{komga}\n"
                f"注册用户：{stats['total']}（禁用 {stats['blocked']}）\n\n"
                "任务统计（全部用户）：\n" + ("\n".join(f"{r[0]}：{r[1]}" for r in rows) or "暂无任务"))
