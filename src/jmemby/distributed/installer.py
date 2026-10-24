"""Self-contained, version-matched agent bundle and one-use enrollment installer."""
from functools import lru_cache
import gzip
import hashlib
import io
from pathlib import Path
import shlex
import tarfile


AGENT_FILES = (
    '__init__.py', 'runtime.py', 'domain/__init__.py', 'domain/models.py', 'domain/errors.py',
    'providers/__init__.py', 'providers/command.py', 'providers/builtin.py', 'providers/jm_adapter.py',
    'storage/__init__.py', 'storage/cbz.py', 'distributed/__init__.py', 'distributed/config.py',
    'distributed/agent.py',
)
DOCKERFILE = '''FROM python:3.12-slim
RUN groupadd --gid 1000 agent && useradd --uid 1000 --gid agent --create-home agent
WORKDIR /app
RUN pip install --no-cache-dir 'aiohttp>=3.10,<4' 'Pillow>=12,<13' 'jmcomic>=2.5,<3'
COPY --chown=agent:agent src ./src
ENV PYTHONPATH=/app/src PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN mkdir /agent-data && chown agent:agent /agent-data
USER agent
ENTRYPOINT ["python", "-m", "jmemby.distributed.agent"]
'''


@lru_cache(maxsize=1)
def agent_bundle():
    root = Path(__file__).resolve().parents[1]
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode='w') as tar:
        contents = [('Dockerfile', DOCKERFILE.encode())]
        contents += [('src/jmemby/' + name, (root / name).read_bytes()) for name in AGENT_FILES]
        for name, content in contents:
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(content), 0o644, 0
            tar.addfile(info, io.BytesIO(content))
    data = gzip.compress(archive.getvalue(), mtime=0)
    return data, hashlib.sha256(data).hexdigest()


def install_command(origin, name, ticket):
    # Each variable crosses a shell boundary only through shlex.quote.
    body = ('set -euo pipefail; [ "$(id -u)" -eq 0 ] || { echo "请以 root 身份执行安装命令"; exit 1; }; '
            'if ! command -v curl >/dev/null; then apt-get update && apt-get install -y curl ca-certificates; fi; '
            'kpa_script=$(mktemp); trap \'rm -f -- "$kpa_script"\' EXIT; '
            'curl --proto "=https" --tlsv1.2 -fsS --connect-timeout 15 --max-time 120 '
            + shlex.quote(origin + '/install/agent.sh') + ' -o "$kpa_script"; '
            'bash "$kpa_script" ' + ' '.join(shlex.quote(v) for v in (origin, name, ticket)))
    return 'bash -c ' + shlex.quote(body)


INSTALL_SCRIPT = r'''#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
[[ $(uname -s) == Linux && $(id -u) == 0 ]] || { echo '请在 Linux VPS 使用 root 执行。' >&2; exit 1; }
[[ $# == 3 ]] || { echo '请从节点管理重新复制完整命令。' >&2; exit 1; }
kpa_origin=$1
kpa_node=$2
kpa_ticket=$3
[[ $kpa_origin == https://* && $kpa_node =~ ^[A-Za-z0-9_-]{1,48}$ && $kpa_ticket =~ ^[A-Za-z0-9_-]{32,256}$ ]] || exit 1
if ! command -v docker >/dev/null || ! command -v python3 >/dev/null; then
    command -v apt-get >/dev/null || { echo '请先安装 Docker、Compose v2 和 Python 3。' >&2; exit 1; }
    apt-get update
    apt-get install -y docker.io python3 ca-certificates
fi
if ! docker compose version >/dev/null 2>&1; then
    command -v apt-cache >/dev/null || { echo '请先安装 Docker Compose v2。' >&2; exit 1; }
    apt-get update
    if apt-cache show docker-compose-v2 >/dev/null 2>&1; then
        apt-get install -y docker-compose-v2
    elif apt-cache show docker-compose-plugin >/dev/null 2>&1; then
        apt-get install -y docker-compose-plugin
    else
        echo '当前软件源没有 Compose v2，请先安装 Docker Compose v2 后重新生成命令。' >&2
        exit 1
    fi
fi
docker info >/dev/null 2>&1 || systemctl start docker
kpa_tmp=$(mktemp -d)
trap 'rm -rf -- "$kpa_tmp"' EXIT
# Ticket exchange is intentionally not retried: it succeeds at most once.
curl --proto '=https' --tlsv1.2 -fsS --connect-timeout 15 --max-time 120 \
    -X POST -H "Authorization: Install $kpa_ticket" \
    "$kpa_origin/install/enroll/$kpa_node" -o "$kpa_tmp/enrollment.json"
unset kpa_ticket
curl --proto '=https' --tlsv1.2 -fsS --connect-timeout 15 --max-time 180 --max-filesize 5242880 \
    "$kpa_origin/install/agent.tar.gz" -o "$kpa_tmp/agent.tar.gz"
kpa_root="/opt/komga-plus-agent/$kpa_node"
mkdir -p -- "$kpa_root"
chmod 700 -- "$kpa_root"
python3 - "$kpa_tmp" "$kpa_root" "$kpa_node" <<'PY'
import hashlib, io, json, os, pathlib, re, sys, tarfile
temporary, root, node = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), sys.argv[3]
enrollment = json.loads((temporary / 'enrollment.json').read_text())
payload = (temporary / 'agent.tar.gz').read_bytes()
checksum = hashlib.sha256(payload).hexdigest()
if checksum != enrollment['bundle_sha256']:
    raise SystemExit('Agent 安装包校验失败，请重新生成命令。')
config = enrollment['config']
if config['AGENT_ID'] != node:
    raise SystemExit('节点 ID 不匹配')
release = root / 'releases' / checksum
release.mkdir(parents=True, exist_ok=True)
with tarfile.open(fileobj=io.BytesIO(payload), mode='r:gz') as archive:
    total = 0
    for entry in archive:
        path = pathlib.PurePosixPath(entry.name)
        total += entry.size
        if (not entry.isfile() or path.is_absolute() or '..' in path.parts or total > 5*1024*1024
                or not (entry.name == 'Dockerfile' or entry.name.startswith('src/jmemby/'))):
            raise SystemExit('Agent 安装包路径无效')
        target = release.joinpath(*path.parts)
        if release.resolve() not in target.resolve().parents:
            raise SystemExit('Agent 安装包超出目标目录')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(archive.extractfile(entry).read())
lines = []
for key in ('AGENT_MASTER_URL', 'AGENT_ID', 'AGENT_TOKEN', 'AGENT_CONCURRENCY'):
    value = str(config[key])
    if not re.fullmatch(r'[A-Za-z0-9:/._\[\]-]+', value):
        raise SystemExit('Agent 配置格式无效')
    lines.append(key + '=' + value)
env = root / '.env.agent'
tmp_env = root / '.env.agent.new'
tmp_env.write_text('\n'.join(lines) + '\n')
os.chmod(tmp_env, 0o600)
os.replace(tmp_env, env)
compose = {'services': {'agent': {
    'build': {'context': './releases/' + checksum}, 'env_file': '.env.agent',
    'environment': {'AGENT_WORK_DIR': '/agent-data'}, 'volumes': ['cache:/agent-data'],
    'restart': 'unless-stopped', 'init': True, 'stop_grace_period': '30s',
    'mem_limit': '1g', 'cpus': 2.0,
    'logging': {'driver': 'json-file', 'options': {'max-size': '10m', 'max-file': '3'}}}},
    'volumes': {'cache': {}}}
(root / 'compose.json').write_text(json.dumps(compose, indent=2))
(root / 'project-name').write_text('kp-agent-' + hashlib.sha256(node.encode()).hexdigest()[:12])
PY
cd -- "$kpa_root"
kpa_project=$(cat project-name)
docker compose -p "$kpa_project" -f compose.json up -d --build
docker compose -p "$kpa_project" -f compose.json ps
echo "Komga Plus Agent 已启动：$kpa_node。请回到网页刷新节点在线状态。"
echo "安装目录：$kpa_root"
'''
