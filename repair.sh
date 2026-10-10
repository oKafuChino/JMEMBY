#!/usr/bin/env bash
set -Eeuo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
[[ $(uname -s) == Linux ]] || { echo '仅支持 Linux VPS。'; exit 1; }
[[ -f .env ]] || { echo '未找到 .env。'; exit 1; }
if [[ $EUID == 0 ]]; then SUDO=(); else SUDO=(sudo); fi
"${SUDO[@]}" docker compose config --quiet
"${SUDO[@]}" docker compose stop app komga
echo '修复服务数据目录权限；如失败，服务保持停止，请检查上方错误。'
"${SUDO[@]}" python3 scripts/repair_permissions.py
"${SUDO[@]}" docker compose up -d --pull never komga
echo 'Komga 已重新启动。请检查 docker compose logs --tail 100 komga。'
echo '若仍有 SQLITE_CANTOPEN，请检查自定义数据库路径、磁盘空间及只读文件系统。'
echo '确认 Komga 页面可用后，执行 bash install.sh 检查配置并启动 Bot。'
