import logging
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from jmemby.domain.errors import JobError
from jmemby.services.diagnostics import Diagnostics, RecentLogs


class DiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    def make_service(self):
        users = Mock()
        users.require_admin.side_effect = lambda actor: None if actor == 1 else (_ for _ in ()).throw(ValueError("admin only"))
        users.statistics.return_value = {"total": 3, "blocked": 1}
        users.store.connection.execute.return_value.fetchall.return_value = [("FAILED", 2)]
        bridge = SimpleNamespace(request=AsyncMock(return_value={}))
        return Diagnostics(users, SimpleNamespace(root=".", heartbeat=time.monotonic()), bridge, RecentLogs())

    async def test_admin_only_before_any_external_access(self):
        service = self.make_service()
        with self.assertRaises(ValueError):
            await service.status(2)
        with self.assertRaises(ValueError):
            service.recent_logs(2)
        service.komga.request.assert_not_called()
        service.users.store.connection.execute.assert_not_called()

    async def test_status_success_and_safe_failure(self):
        service = self.make_service()
        text = await service.status(1)
        self.assertIn("FAILED：2", text)
        self.assertIn("连接及漫画库检查通过", text)
        service.komga.request.side_effect = JobError("KOMGA_AUTH", "private credential")
        text = await service.status(1)
        self.assertIn("KOMGA_AUTH", text)
        self.assertNotIn("private credential", text)
        service.komga.request.side_effect = TimeoutError()
        self.assertIn("检查超时", await service.status(1))

    def test_log_limit_redaction_and_single_line_records(self):
        with patch.dict("os.environ", {"KOMGA_PASSWORD": "private-value"}):
            logs = RecentLogs()
        for i in range(105):
            logs.handle(logging.LogRecord("test", logging.INFO, "", 0, "event %s", (i,), None))
        logs.handle(logging.LogRecord("test", logging.WARNING, "", 0,
                                      "private-value password=dynamic-secret https://host/private\nline", (), None))
        output = logs.snapshot()
        self.assertEqual(len(output.splitlines()), 100)
        self.assertNotIn("event 5\n", output)
        self.assertNotIn("private-value", output)
        self.assertNotIn("dynamic-secret", output)
        self.assertNotIn("host/private", output)
        self.assertIn("event 104", output)
