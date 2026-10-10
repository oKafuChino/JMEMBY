#!/usr/bin/env bash
# Run from a checked-out release: bash install.sh
set -Eeuo pipefail
trap 'echo "安装未完成（行 $LINENO）。已有配置和数据已保留，修复后可重新运行。" >&2' ERR
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
[[ $(uname -s) == Linux ]] || { echo '仅支持 Linux VPS。' >&2; exit 1; }
[[ -t 0 ]] || { echo '请在交互终端运行 bash install.sh。' >&2; exit 1; }
if [[ $EUID == 0 ]]; then SUDO=(); else SUDO=(sudo); command -v sudo >/dev/null; fi
if ! command -v docker >/dev/null || ! command -v python3 >/dev/null; then
  command -v apt-get >/dev/null || { echo '请先安装 Docker Engine、Compose v2 和 Python 3。'; exit 1; }
  "${SUDO[@]}" apt-get update
  "${SUDO[@]}" apt-get install -y docker.io python3
fi
if ! "${SUDO[@]}" docker compose version >/dev/null 2>&1; then
  command -v apt-cache >/dev/null || { echo '请安装 Docker Compose v2 后重试。'; exit 1; }
  if apt-cache show docker-compose-v2 >/dev/null 2>&1; then
    "${SUDO[@]}" apt-get install -y docker-compose-v2
  elif apt-cache show docker-compose-plugin >/dev/null 2>&1; then
    "${SUDO[@]}" apt-get install -y docker-compose-plugin
  else
    echo '当前软件源没有 Compose v2，请按 https://docs.docker.com/engine/install/ 安装后重试。'
    exit 1
  fi
fi
if ! "${SUDO[@]}" docker info >/dev/null 2>&1; then
  "${SUDO[@]}" systemctl start docker
fi
python3 scripts/configure.py initial
"${SUDO[@]}" docker compose config --quiet
# Create only our owned data directories. Never recursively chown existing data.
read -r APP_UID APP_GID < <(python3 scripts/configure.py ids)
for directory in data data/app data/komga data/storage data/storage/library data/storage/staging; do
  if [[ ! -e "$directory" ]]; then
    "${SUDO[@]}" install -d -m 750 -o "$APP_UID" -g "$APP_GID" "$directory"
  fi
done
"${SUDO[@]}" docker compose up -d komga
echo 'Komga 已启动。首次使用请在另一个终端建立隧道：'
echo '  ssh -L 25600:127.0.0.1:25600 用户名@VPS地址'
echo '浏览器打开 http://localhost:25600，创建管理员账户和根目录为 /data 的漫画库。'
echo '已有账户/漫画库无需重新创建。等待界面可访问后继续。'
read -r -p '按 Enter 填写 Komga 凭据和漫画库 ID：' _
python3 scripts/configure.py komga
"${SUDO[@]}" docker compose config --quiet
"${SUDO[@]}" docker compose build app
"${SUDO[@]}" docker compose run --rm app check-config
"${SUDO[@]}" docker compose run --rm app check-integrations
"${SUDO[@]}" docker compose up -d app
"${SUDO[@]}" docker compose ps
echo '部署命令已完成。请用 Telegram 提交一个小漫画验收真实下载和入库。'
echo '排查日志：sudo docker compose logs --tail 100 app'
