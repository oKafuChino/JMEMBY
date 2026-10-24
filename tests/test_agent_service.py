import asyncio
import logging
from pathlib import Path
import signal
import tempfile
import unittest
from unittest.mock import patch

from jmemby.distributed.agent import log_handler, run_service


class NativeServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_sigterm_drains_agent_before_returning(self):
        started, drained = asyncio.Event(), asyncio.Event()
        class Agent:
            async def run(self):
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    drained.set()
        callbacks = {}
        loop = asyncio.get_running_loop()
        with patch.object(loop, 'add_signal_handler', side_effect=lambda sig, fn: callbacks.update({sig:fn})), \
                patch.object(loop, 'remove_signal_handler') as remove:
            work = asyncio.create_task(run_service(Agent()))
            try:
                await asyncio.wait_for(started.wait(), 1)
                callbacks[signal.SIGTERM]()
                await asyncio.wait_for(work, 1)
                self.assertTrue(drained.is_set())
                self.assertEqual(remove.call_count, 2)
            finally:
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)

    async def test_service_does_not_hide_startup_failure(self):
        class BrokenAgent:
            async def run(self):
                raise RuntimeError('failed')
        with self.assertRaisesRegex(RuntimeError,'failed'):
            await run_service(BrokenAgent())

    def test_native_logs_rotate_and_default_remains_stderr(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'agent.log'
            with patch.dict('os.environ',{'AGENT_LOG_FILE':str(path)}):
                handler = log_handler()
            try:
                self.assertEqual(handler.maxBytes,2*1024*1024)
                self.assertEqual(handler.backupCount,2)
                handler.maxBytes=10
                for _ in range(6):
                    handler.emit(logging.LogRecord('test',logging.INFO,'test',1,'message'*3,(),None))
                self.assertLessEqual(len(list(Path(directory).glob('agent.log*'))),3)
            finally:
                handler.close()
        with patch.dict('os.environ',{},clear=True):
            self.assertIsInstance(log_handler(),logging.StreamHandler)
