#!/bin/sh
# CI-only dependency smoke test in an isolated distro container, no enrollment.
set -eu
. /etc/os-release
case "$ID" in
    debian|ubuntu)
        apt-get update
        apt-get install -y --no-install-recommends python3 python3-venv ca-certificates
        python3 -m venv /tmp/agent-venv
        ;;
    alpine)
        apk add --no-cache python3 py3-pip py3-virtualenv ca-certificates
        python3 -m virtualenv /tmp/agent-venv
        ;;
    *) exit 1;;
esac
export PYTHONPATH=/workspace/src
python3 - <<'PY'
from pathlib import Path
from jmemby.distributed.installer import agent_bundle, INSTALL_SCRIPT
from jmemby.distributed.native_install import prepare
temporary = Path('/tmp/agent-package')
temporary.mkdir()
(temporary / 'agent.tar.gz').write_bytes(agent_bundle()[0])
release = prepare(temporary, Path('/tmp/agent-install'))
Path('/tmp/agent-release').write_text(str(release))
Path('/tmp/agent-installer.sh').write_text(INSTALL_SCRIPT)
PY
sh -n /tmp/agent-installer.sh
agent_release=$(cat /tmp/agent-release)
/tmp/agent-venv/bin/python -m pip install --disable-pip-version-check --no-cache-dir --upgrade pip
/tmp/agent-venv/bin/python -m pip install --disable-pip-version-check --no-cache-dir \
    --only-binary=:all: -r "$agent_release/requirements-agent.txt"
export PYTHONPATH="$agent_release/src"
/tmp/agent-venv/bin/python - <<'PY'
import sys
import aiohttp, PIL, jmcomic
from jmemby.distributed.agent import Agent, run_service
from jmemby.distributed.config import AgentSettings
assert sys.version_info >= (3, 11)
assert AgentSettings('https://example.com', 'test', 'a' * 43).concurrency == 1
print('Native agent dependency/import smoke test passed:', sys.version)
PY
