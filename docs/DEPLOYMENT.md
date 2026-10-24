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

应用健康检查检查事件循环/worker 心跳，依赖异常通过任务状态体现。镜像不会因为一次网络中断主动重启。通过 `/web` 下载队列及原聊天的完成通知判断业务结果。

## `/jm` 与群聊支持升级

同步代码后运行 `docker compose up -d --build --no-deps app` 重建应用。本次命令和群聊改动不新增数据库迁移（schema 仍为 6）。原 `/download` 替换为 `/jm`，私聊直接发送 ID/链接仍有效。若曾在 BotFather 手动设置旧命令列表，请将 `download` 更新为 `jm`。

用户先在私聊注册，将机器人加入群并允许发送消息，再用 `/jm@机器人用户名 漫画ID` 验证直接入队、群内进度和完成通知。普通群和超级群均支持；群内其他命令和纯 ID/链接不会响应，账号、凭据、任务管理及管理员功能仅私聊。检查他人点击确认/取消不会创建任务或删除预览；重复提交已有私聊任务时，通知应继续发回原私聊。

## 备份和恢复

双队列版本将数据库升级至 schema 8，新增任务优先级并保留原入队顺序；升级时已有白名单用户的任务归入优先队列。节点管理版本继续升级至 schema 9，持久化节点及凭据哈希。本版继续升级至 schema 10，增加节点服务队列设置，已有节点默认服务普通和白名单两种队列。该设置由主控调度，无需升级 Agent。漫画列表版本继续升级至 schema 11，保留漫画移除时间与下载历史，支持删除后重新下载。先备份数据库，再执行 `docker compose up -d --build --no-deps app`。验证白名单任务优先、普通任务被白名单重复提交后移动至优先队列末尾，且任务总数和原任务通知位置不变。运行中的任务不被抢占；回滚至旧版本需要恢复升级前数据库。远程下载强制启用，主控默认 Compose 已映射 Agent API 端口，无需额外 overlay；节点网页安装流程见 [多 VPS 下载](DISTRIBUTED_DOWNLOADS.md)。

当前用户组功能将数据库升级到 schema 7：新增普通/白名单组别及注册资格记录。更新前备份数据库，执行 `docker compose up -d --build --no-deps app` 后自动迁移。旧用户默认普通组。管理员在群中回复用户消息验证 `/kk`、`/white`，并检查普通用户无法授权、私聊无法执行这两个命令。具体规则见 [注册与用户组](REGISTRATION.md)。

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

## 自动下载功能移除升级（2026-10-24）

新版仅保留用户发送漫画 ID/链接并确认的下载流程。更新前停止旧 app，备份 `data/app`（包含数据库及 account.key）；不要删除漫画存储目录。完成备份并同步代码后执行：

```bash
docker compose up -d --build --no-deps app
docker compose logs --tail=100 app
```

启动会自动迁移 schema 1–5 到 schema 6。对 schema 5：取消所有未完成的自动来源任务，停止其待发送通知，删除自动计划及相关表、任务上的自动来源字段和五分钟等待状态。已完成/失败/取消的历史任务、已有文件及章节路径、用户、邀请码、Komga 账号均保留。手动任务继续正常恢复、取消及重试；不自动删除已下载或部分下载的文件。

`SCHEDULE_DEFAULT_TIMEZONE` 已停用，旧 `.env` 中保留该变量也不会影响启动，可自行删除。没有调度循环、标签自动搜索或自动通知服务，健康检查只依赖下载 worker。运行一个 app 实例即可。

Telegram 发送 `/start` 刷新菜单。旧定时命令与按钮只显示功能已移除，不会创建任务。核对网页任务状态、手动发送 ID 后的预览/确认、下载及 Komga 入库；同时检查旧自动队列不再继续执行。测试记录见 [功能移除记录](AUTO_DOWNLOAD_REMOVAL.md)。

迁移不可通过换回旧镜像直接回滚：旧版不支持 schema 6，计划配置已删除；如需回滚必须停止新版并恢复升级前的数据库与密钥备份。

## Bot 互动版本升级

本版升级到 schema 12，新增点赞、举报及通知投递表。升级前备份，再执行 `docker compose up -d --build --no-deps app`。`/jm` 直接下载，移除旧队列操作命令，`/my` 显示阅读账号与服务器地址；新指令和验证事项见 [Bot 指令说明](BOT_COMMANDS.md)。管理员需先私聊启动 Bot 才能接收举报。仅更新主控，无需升级 Agent。

## 网页举报审核与批量静默升级

本版升级至 schema 14，新增任务最后执行节点记录与提交时间排序索引，升级前的历史节点无法补录。下载队列按提交时间从新到旧显示并自动刷新，实际调度仍是白名单优先。旧举报保留并设为待处理；旧任务通知策略不变，新批量任务默认静默。备份后更新主控，验证举报页管理员权限、忽略/移除处理记录、批量任务无 Bot 推送以及 /jm 任务正常推送。重启后复核处理结果与静默标记持久化。无需升级 Agent。
