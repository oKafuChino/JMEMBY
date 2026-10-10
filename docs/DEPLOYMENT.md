# VPS 部署

更新：已提供内置 JMComic/Komga 适配器与 `bash install.sh` 交互安装入口；无需自行编写适配器。以下旧说明中“先实现适配器”的前置条件已被替代。真实上游兼容与容器运行仍未验证，以 README 当前安装步骤为准。

## 前置条件

Linux VPS、Docker Engine 和 Compose 插件。Komga 默认拉取 gotson/komga:latest，无需填写版本号；先执行连接检查再提交小样本验收。

复制环境示例，配置白名单、Token、Komga 凭据和公共 URL。`.env` 仅授权部署用户读取，不加入版本库。

创建持久化目录，UID/GID 与 `.env` 中 APP_UID/APP_GID 一致：

```sh
mkdir -p data/app data/komga data/storage/library data/storage/staging
sudo chown -R 1000:1000 data
chmod 600 .env
docker compose config --quiet
docker compose up -d komga
```

这里的 chown 只用于新建项目的 data 目录；已有数据目录应检查原所有者后调整。

Komga 默认仅监听宿主机回环端口，通过 SSH 隧道访问初始界面，创建账户和指向 `/data` 的 library，然后将 ID 和鉴权信息填回 `.env`。Bot 容器使用 `http://komga:25600`，用户入口使用独立 KOMGA_PUBLIC_URL。

```sh
docker compose build app
docker compose run --rm app check-config
docker compose up -d app
docker compose logs --tail 100 app
```

镜像通过 pyproject 安装 jmcomic，默认命令使用内置适配器。建议小漫画验证通过后再配置 HTTPS 反向代理或 VPN。默认 library 只读挂载到 Komga，启用需要写入的功能前验证对应版本需求。

应用健康检查检查事件循环/worker 心跳，依赖异常通过任务状态体现。镜像不会因为一次网络中断主动重启。查看 `/status` 判断业务结果。

## 备份和恢复

应用数据库使用 SQLite 在线备份，目标文件必须不存在：

```sh
docker compose exec app python -m jmemby.main backup --destination /app-data/backup-2026-10-10.db
```

将备份文件复制至异地存储。Komga 配置与数据库在停机后做一致性备份；漫画库按容量预算单独备份。真实凭据备份应加密。

恢复时停止 app/komga，用备份还原相应数据库和文件，保持目录映射和所有者一致。SQLite 恢复到空目录中，避免残留旧 WAL/SHM。先启动 Komga，再启动 app，检查已完成漫画和一个新任务。不要把新 schema 数据库直接交给旧镜像；回滚可能同时需要恢复升级前数据库。

## 资源与限制

磁盘阈值以 GiB 计算。staging 与 library 必须同文件系统以支持原子硬链接发布。底层文件系统必须支持硬链接。不要挂载为两个独立文件系统。

当前打包及文件完整性校验为同步操作，大章节可能短时阻塞 Bot；在正式发布前需要改为可控的打包执行单元并验证取消边界。暂不自动清理 staging，成功/失败任务的临时文件需按验收记录安排人工检查；不要删除运行中任务的目录。

Docker Compose 和镜像构建尚未在当前机器验证（本机无 Docker）。CPU/内存上限需要在目标 VPS 检查是否实际生效。
