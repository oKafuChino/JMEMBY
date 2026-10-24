"""Run as root in the distro CI containers to exercise real Unix user permissions."""
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

from jmemby.distributed.native_install import prepare_state


@unittest.skipUnless(os.name == 'posix' and getattr(os, 'geteuid', lambda: -1)() == 0,
                     'requires a root Linux container for service-user regression tests')
class NativePermissionTests(unittest.TestCase):
    def test_restrictive_umask_reproduces_failure_and_repair_allows_service_user(self):
        import pwd
        account=pwd.getpwnam('nobody')
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);base.chmod(0o755)
            state=base/'komga-plus-agent'/'node'
            mask=os.umask(0o077)
            try:
                state.mkdir(parents=True)
                os.chown(state,account.pw_uid,account.pw_gid)
                script='from pathlib import Path; import os,sys; os.chdir(sys.argv[1]); Path("agent.log").write_text("ready"); Path("agent.lock").touch()'
                args=[sys.executable,'-c',script,str(state)]
                kwargs=dict(user=account.pw_uid,group=account.pw_gid,extra_groups=[],capture_output=True)
                failed=subprocess.run(args,**kwargs)
                self.assertNotEqual(failed.returncode,0)
                self.assertIn(b'PermissionError',failed.stderr)
                prepare_state(state,'nobody')
                repaired=subprocess.run(args,**kwargs)
                self.assertEqual(repaired.returncode,0,repaired.stderr)
                self.assertEqual(stat.S_IMODE(state.parent.stat().st_mode),0o755)
                self.assertEqual(stat.S_IMODE(state.stat().st_mode),0o700)
                self.assertEqual((state/'agent.log').read_text(),'ready')
                # Re-running a repair preserves private data and permissions.
                prepare_state(state,'nobody')
                self.assertEqual((state/'agent.log').read_text(),'ready')
                self.assertEqual(stat.S_IMODE(state.stat().st_mode),0o700)
            finally:
                os.umask(mask)
