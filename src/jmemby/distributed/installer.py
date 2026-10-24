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
REQUIREMENTS = "aiohttp>=3.10,<4\nPillow>=12,<13\njmcomic>=2.5,<3\n"


@lru_cache(maxsize=1)
def agent_bundle():
    root = Path(__file__).resolve().parents[1]
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode='w') as tar:
        contents = [('requirements-agent.txt', REQUIREMENTS.encode())]
        contents += [('src/jmemby/' + name, (root / name).read_bytes()) for name in AGENT_FILES]
        for name, content in contents:
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(content), 0o644, 0
            tar.addfile(info, io.BytesIO(content))
    data = gzip.compress(archive.getvalue(), mtime=0)
    return data, hashlib.sha256(data).hexdigest()


def install_command(origin, name, ticket):
    # POSIX sh is available on all supported systems, including stock Alpine.
    body = ('set -eu; umask 077; [ "$(id -u)" -eq 0 ] || { echo "请以 root 身份执行"; exit 1; }; '
            '. /etc/os-release; case "$ID" in debian|ubuntu) '
            'if ! command -v curl >/dev/null; then apt-get update && apt-get install -y --no-install-recommends curl ca-certificates; fi;; '
            'alpine) apk add --no-cache curl ca-certificates;; '
            '*) echo "仅支持 Debian、Ubuntu、Alpine"; exit 1;; esac; '
            'kpa_script=$(mktemp); trap \'rm -f -- "$kpa_script"\' EXIT; '
            'curl --proto "=https" --tlsv1.2 -fsS --connect-timeout 15 --max-time 120 '
            + shlex.quote(origin + '/install/agent.sh') + ' -o "$kpa_script"; '
            'sh "$kpa_script" ' + ' '.join(shlex.quote(v) for v in (origin, name, ticket)))
    return 'sh -c ' + shlex.quote(body)


INSTALL_SCRIPT = r'''#!/bin/sh
set -eu
umask 077
[ "$(uname -s)" = Linux ] && [ "$(id -u)" -eq 0 ] || { echo '请在 Linux VPS 使用 root 执行。' >&2; exit 1; }
[ "$#" -eq 3 ] || { echo '请复制完整安装命令。' >&2; exit 1; }
kpa_origin=$1
kpa_node=$2
kpa_ticket=$3
case "$kpa_origin" in https://*) ;; *) exit 1;; esac
case "$kpa_node" in ''|*[!A-Za-z0-9_-]*) exit 1;; esac
[ "${#kpa_node}" -le 48 ] || exit 1
case "$kpa_ticket" in ''|*[!A-Za-z0-9_-]*) exit 1;; esac
[ "${#kpa_ticket}" -ge 32 ] && [ "${#kpa_ticket}" -le 256 ] || exit 1
. /etc/os-release
case "$ID" in
    debian|ubuntu)
        [ -d /run/systemd/system ] && command -v systemctl >/dev/null || {
            echo 'Debian/Ubuntu VPS 需要运行 systemd。' >&2; exit 1;
        }
        kpa_init=systemd
        apt-get update
        apt-get install -y --no-install-recommends python3 python3-venv curl ca-certificates
        ;;
    alpine)
        command -v rc-service >/dev/null && command -v supervise-daemon >/dev/null || {
            echo 'Alpine VPS 需要运行 OpenRC（不是普通的 Alpine 应用容器）。' >&2; exit 1;
        }
        kpa_init=openrc
        apk add --no-cache python3 py3-pip py3-virtualenv curl ca-certificates
        ;;
    *) echo '仅支持 Debian、Ubuntu、Alpine。' >&2; exit 1;;
esac
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else "需要 Python 3.11+；请使用 Debian 12+、Ubuntu 24.04+ 或 Alpine 3.20+。")'
kpa_tmp=$(mktemp -d)
trap 'rm -rf -- "$kpa_tmp"' EXIT
cat > "$kpa_tmp/native_install.py" <<'KPA_PY'
''' + Path(__file__).with_name('native_install.py').read_text(encoding='utf-8') + r'''
KPA_PY
curl --proto '=https' --tlsv1.2 -fsS --connect-timeout 15 --max-time 180 --max-filesize 5242880 \
    "$kpa_origin/install/agent.tar.gz" -o "$kpa_tmp/agent.tar.gz"
kpa_root="/opt/komga-plus-agent/$kpa_node"
mkdir -p -- "$kpa_root"
kpa_release=$(python3 "$kpa_tmp/native_install.py" prepare "$kpa_tmp" "$kpa_root")
# Prepare dependencies before rotating credentials or stopping an existing Agent.
if [ ! -x "$kpa_release/venv/bin/python" ]; then
    if [ "$kpa_init" = openrc ]; then
        python3 -m virtualenv "$kpa_release/venv"
    else
        python3 -m venv "$kpa_release/venv"
    fi
fi
"$kpa_release/venv/bin/python" -m pip install --disable-pip-version-check --no-cache-dir --upgrade pip
# Binary wheels avoid retaining compilers and build toolchains on small VPSes.
"$kpa_release/venv/bin/python" -m pip install --disable-pip-version-check --no-cache-dir \
    --only-binary=:all: -r "$kpa_release/requirements-agent.txt"
PYTHONPATH="$kpa_release/src" "$kpa_release/venv/bin/python" -c 'import aiohttp, PIL, jmcomic; import jmemby.distributed.agent'
# Ticket exchange is not retried: each installation credential succeeds once.
curl --proto '=https' --tlsv1.2 -fsS --connect-timeout 15 --max-time 120 \
    -X POST -H "Authorization: Install $kpa_ticket" \
    "$kpa_origin/install/enroll/$kpa_node" -o "$kpa_tmp/enrollment.json"
unset kpa_ticket
kpa_service=$(python3 "$kpa_tmp/native_install.py" configure "$kpa_tmp" "$kpa_root" "$kpa_node")
kpa_state="/var/lib/komga-plus-agent/$kpa_service"
if ! id "$kpa_service" >/dev/null 2>&1; then
    if [ "$kpa_init" = openrc ]; then
        addgroup -S "$kpa_service"
        adduser -S -D -H -h "$kpa_state" -s /sbin/nologin -G "$kpa_service" "$kpa_service"
    else
        useradd --system --user-group --home-dir "$kpa_state" --no-create-home --shell /usr/sbin/nologin "$kpa_service"
    fi
fi
[ "$(id -u "$kpa_service")" -ne 0 ] || exit 1
mkdir -p -- "$kpa_state"
chown "$kpa_service:$kpa_service" "$kpa_state"
chmod 700 "$kpa_state"
chown "root:$kpa_service" "$kpa_root"
chmod 750 "$kpa_root"
chmod 755 /opt/komga-plus-agent "$kpa_root/releases"
chmod -R a+rX "$kpa_release"
chown "$kpa_service:$kpa_service" "$kpa_root/.env.agent.new"
# Migrate only the old installer-owned Docker project, never unrelated containers.
if [ -f "$kpa_root/compose.json" ] && [ -f "$kpa_root/project-name" ]; then
    kpa_project=$(cat "$kpa_root/project-name")
    case "$kpa_project" in kp-agent-*) ;; *) echo '旧 Docker 项目名无效' >&2; exit 1;; esac
    case "$kpa_project" in *[!a-z0-9-]*) exit 1;; esac
    docker compose -p "$kpa_project" -f "$kpa_root/compose.json" stop agent
    mv "$kpa_root/compose.json" "$kpa_root/compose.docker-backup.json"
fi
if [ "$kpa_init" = systemd ]; then
    if [ -f "/etc/systemd/system/$kpa_service.service" ]; then systemctl stop "$kpa_service"; fi
else
    if [ -f "/etc/init.d/$kpa_service" ]; then rc-service "$kpa_service" stop; fi
fi
mv -f "$kpa_root/.env.agent.new" "$kpa_root/.env.agent"
mv -f "$kpa_root/run-agent.new" "$kpa_root/run-agent"
ln -sfn "$kpa_release" "$kpa_root/current.new"
mv -Tf "$kpa_root/current.new" "$kpa_root/current"
if [ "$kpa_init" = systemd ]; then
    cp "$kpa_root/agent.service" "/etc/systemd/system/$kpa_service.service"
    systemctl daemon-reload
    systemctl enable "$kpa_service"
    systemctl restart "$kpa_service"
    systemctl is-active --quiet "$kpa_service"
else
    cp "$kpa_root/agent.openrc" "/etc/init.d/$kpa_service"
    chmod 755 "/etc/init.d/$kpa_service"
    rc-update add "$kpa_service" default
    rc-service "$kpa_service" start
    rc-service "$kpa_service" status
fi
echo "Komga Plus Agent 已启动：$kpa_node。请回网页刷新节点状态。"
echo "服务名：$kpa_service；日志：$kpa_state/agent.log；安装目录：$kpa_root"
'''
