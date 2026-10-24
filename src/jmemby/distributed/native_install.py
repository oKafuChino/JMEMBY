"""Standard-library-only preparation for the native Linux Agent installer.

The controller embeds this file in the shell installer. Preparing a release never
changes the running service, credentials or current symlink.
"""
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import sys
import tarfile


def service_name(node):
    return 'kpa-' + hashlib.sha256(node.encode()).hexdigest()[:12]


def write_private(path, content, mode=0o600):
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w', encoding='utf-8', newline='\n') as stream:
        stream.write(content)
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def prepare(temporary, root):
    payload = (temporary / 'agent.tar.gz').read_bytes()
    if len(payload) > 5 * 1024 * 1024:
        raise ValueError('Agent 安装包过大')
    checksum = hashlib.sha256(payload).hexdigest()
    release = root / 'releases' / checksum
    release.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(payload), mode='r:gz') as archive:
        total, seen = 0, set()
        for entry in archive:
            path = PurePosixPath(entry.name)
            total += entry.size
            if (not entry.isfile() or path.is_absolute() or '..' in path.parts
                    or '\\' in entry.name or entry.name in seen or total > 5*1024*1024
                    or not (entry.name == 'requirements-agent.txt'
                            or entry.name.startswith('src/jmemby/') and entry.name.endswith('.py'))):
                raise ValueError('Agent 安装包路径无效')
            seen.add(entry.name)
            target = release.joinpath(*path.parts)
            if release.resolve() not in target.resolve().parents:
                raise ValueError('Agent 安装包超出目标目录')
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.extractfile(entry).read())
            os.chmod(target, 0o644)
    required = {'requirements-agent.txt', 'src/jmemby/distributed/agent.py'}
    if not required <= seen:
        raise ValueError('Agent 安装包缺少必要文件')
    return release


def configure(temporary, root, node):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,48}', node):
        raise ValueError('节点 ID 无效')
    enrollment = json.loads((temporary / 'enrollment.json').read_text(encoding='utf-8'))
    checksum = hashlib.sha256((temporary / 'agent.tar.gz').read_bytes()).hexdigest()
    if checksum != enrollment['bundle_sha256']:
        raise ValueError('主控安装包版本已变更，请重新生成安装命令')
    config = enrollment['config']
    if config['AGENT_ID'] != node:
        raise ValueError('节点 ID 不匹配')
    name = service_name(node)
    state = '/var/lib/komga-plus-agent/' + name
    values = {key: str(config[key]) for key in (
        'AGENT_MASTER_URL', 'AGENT_ID', 'AGENT_TOKEN', 'AGENT_CONCURRENCY')}
    if any(not re.fullmatch(r'[A-Za-z0-9:/._\[\]-]+', value) for value in values.values()):
        raise ValueError('Agent 配置格式无效')
    values.update(AGENT_WORK_DIR=state, AGENT_LOG_FILE=state + '/agent.log', AGENT_LOW_MEMORY='auto')
    write_private(root / '.env.agent.new', ''.join(key + '=' + value + '\n' for key, value in values.items()))
    # All paths below are installer-owned, not writable by the service account.
    command = shlex.quote(str(root / 'current/venv/bin/python'))
    launcher = ('#!/bin/sh\nset -eu\nset -a\n. ' + shlex.quote(str(root / '.env.agent'))
                + '\nset +a\nexport PYTHONPATH=' + shlex.quote(str(root / 'current/src'))
                + '\numask 077\nmkdir -p "$AGENT_WORK_DIR/tmp"\nexport TMPDIR="$AGENT_WORK_DIR/tmp"'
                + '\nexport PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1\nexec '
                + command + ' -m jmemby.distributed.agent\n')
    write_private(root / 'run-agent.new', launcher, 0o755)
    unit = f'''[Unit]
Description=Komga Plus download Agent ({node})
Wants=network-online.target
After=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
User={name}
Group={name}
WorkingDirectory={state}
ExecStart={root}/run-agent
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
KillMode=control-group
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths={state}
UMask=0077

[Install]
WantedBy=multi-user.target
'''
    openrc = f'''#!/sbin/openrc-run
description="Komga Plus download Agent ({node})"
supervisor="supervise-daemon"
command={shlex.quote(str(root / 'run-agent'))}
command_user="{name}:{name}"
directory="{state}"
respawn_delay=5
respawn_max=0
retry="TERM/30/KILL/5"
depend() {{
    need net
    after firewall
}}
'''
    write_private(root / 'agent.service', unit, 0o644)
    write_private(root / 'agent.openrc', openrc, 0o755)
    write_private(root / 'service-name', name + '\n')
    return name


def prepare_state(state, service):
    """Keep the shared parent traversable and each node's data private."""
    import pwd
    account = pwd.getpwnam(service)
    state.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(state.parent, 0o755)
    state.mkdir(exist_ok=True)
    os.chown(state, account.pw_uid, account.pw_gid)
    os.chmod(state, 0o700)


def check_access(root, state, service):
    """Check the installed runtime as its real user, without contacting a server."""
    import pwd
    account = pwd.getpwnam(service)
    if account.pw_uid == 0:
        raise ValueError('Agent 服务用户不能为 root')
    script = '''import os, pathlib, sys, tempfile
import aiohttp, PIL, jmcomic
from jmemby.distributed.config import AgentSettings
from jmemby.distributed.agent import Agent, log_handler
from jmemby.runtime import instance_lock
config = pathlib.Path(sys.argv[1]).read_text(encoding='utf-8')
os.environ.update(line.split('=', 1) for line in config.splitlines() if line)
os.chdir(os.environ['AGENT_WORK_DIR'])
agent = Agent(AgentSettings.from_env())
with instance_lock(agent.root / 'agent.lock'):
    with tempfile.TemporaryDirectory(prefix='preflight-', dir=agent.root):
        pass
    handler = log_handler()
    handler.close()
'''
    environment = {**os.environ, 'PYTHONPATH': str(root / 'current/src'),
                   'PYTHONDONTWRITEBYTECODE': '1'}
    subprocess.run([str(root / 'current/venv/bin/python'), '-c', script,
                    str(root / '.env.agent')], cwd=state, env=environment,
                   user=account.pw_uid, group=account.pw_gid, extra_groups=[], check=True)


if __name__ == '__main__':
    operation, temporary, root = sys.argv[1:4]
    if operation == 'prepare':
        print(prepare(Path(temporary), Path(root)))
    elif operation == 'configure':
        print(configure(Path(temporary), Path(root), sys.argv[4]))
    elif operation == 'check-access':
        # CLI arguments here are root, state and service, not staging paths.
        check_access(Path(temporary), Path(root), sys.argv[4])
    elif operation == 'prepare-state':
        prepare_state(Path(temporary), root)
    else:
        raise SystemExit('Unknown installation phase')
