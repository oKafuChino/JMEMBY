# Komga Plus 多 VPS 下载

## 工作方式

**每台附属 VPS 同时处理一本漫画，多台 VPS 并行处理不同漫画。**

- 主控保留 Telegram Bot、管理员网页、SQLite、漫画库和 Komga。
- 附属 VPS 只运行下载 Agent，不运行 Bot、Komga 或数据库，不挂载主控目录，也不开放入站端口。
- Agent 主动通过 HTTPS 向主控请求工作、续约和回传图片。每个节点使用独立 ID 与 Token。
- 主控按既有白名单优先、组内 FIFO 规则为在线空闲节点领取任务。正在处理的漫画不被后来提交的任务抢占。
- 一本漫画从开始下载到全部章节打包、入库确认，固定占用同一节点；期间该节点不会领取第二本。
- 一本内部的图片允许并发下载。`AGENT_CONCURRENCY` 控制这一本内部的并发下载组数，不是同时下载的漫画数量。每组最多 8 页，组内复用 SDK 客户端。
- 图片按章节回传，主控验证 SHA-256、图片格式、像素数和大小，再生成 CBZ、通知 Komga。最终文件和原始下载用户记录仍保存在主控。

例如 3 台在线 VPS 各处理 1 本，共 3 本并行；每台设置 `AGENT_CONCURRENCY=2`，表示每一本内最多 2 组同时下载，不会把一本分给多台 VPS。

此版本主控仍负责获取作品信息和章节页清单，因此主控需要能访问 JM 元数据接口。仅支持内置 JM 适配器；开启分布式模式时不能设置 `PROVIDER_COMMAND`。

## 主控配置

在已有项目目录更新代码，升级前停止 app 并备份持久化数据库（包含 WAL/SHM，或使用 SQLite backup）。本次启动会自动将数据库升级到 **schema 9**，保存节点和安装凭据哈希；旧程序不能直接打开升级后的数据库，回滚需恢复配套备份。

首次使用在主控 `.env` 配置：

```dotenv
WEB_ENABLED=true
WEB_PUBLIC_URL=https://admin.example.com
DISTRIBUTED_ENABLED=true
DOWNLOAD_AGENT_TOKENS={}
DISTRIBUTED_PORT=8081
DISTRIBUTED_LEASE_SECONDS=90
DISTRIBUTED_JOB_TIMEOUT=1800
```

允许从零节点启动，随后在网页创建节点，最多 32 个。已有 `DOWNLOAD_AGENT_TOKENS` 节点首次启动会导入数据库，之后该变量仅用于补充从未导入过的 ID，不能覆盖已有节点密钥；网页删除的节点也不会在重启时被该变量恢复。新增、编辑、启停和删除节点均无需重启主控。

启动主控时增加分布式 Compose 配置：

```bash
docker compose -f compose.yaml -f compose.distributed.yaml up -d --build --no-deps app
```

后续更新主控也应携带两个 `-f` 参数。继续使用原部署目录；如以前指定了 Compose 项目名，继续传入相同的 `-p` 参数。

Agent API 使用独立端口，不依赖 `WEB_ENABLED`。Compose 只在主控本机 `127.0.0.1:8081` 暴露该端口。网页管理端口仍为 8080。

### HTTPS 反向代理

给 Agent API 配置专用 HTTPS 域名，例如 `agents.example.com`。在**已配置有效 TLS 证书**的 Nginx `server` 中加入：

```nginx
location /v1/agent/ {
    proxy_pass http://127.0.0.1:8081;
    proxy_http_version 1.1;
    proxy_request_buffering off;
    proxy_read_timeout 150s;
    proxy_send_timeout 150s;
    client_max_body_size 32m;
}
location /install/ {
    proxy_pass http://127.0.0.1:8081;
    proxy_http_version 1.1;
    proxy_read_timeout 150s;
    proxy_cache off;
    client_max_body_size 4k;
}
location / {
    return 404;
}
```

`client_max_body_size` 至少覆盖 `MAX_PAGE_BYTES`（默认 30 MiB）。若修改了主控宿主机 `DISTRIBUTED_PORT`，同时调整 `proxy_pass` 端口。不要将 API 转发至网页管理端口。

Agent 验证 TLS 证书，拒绝非本机明文 HTTP 和重定向。公网域名必须直达 API，不能经登录跳转页。只有 `localhost`、`127.0.0.1`、`::1` 的 HTTP 地址允许用于自行建立的 SSH 隧道；普通 Docker 网络名及公网 IP 的 HTTP 地址不被接受。

## 网页生成一键安装命令

1. 管理员私聊 Bot 使用 `/web` 登录管理面板，打开侧边栏 **节点管理**。
2. 保存 Agent HTTPS 地址，例如 `https://agents.example.com`。这是独立的 Agent API 入口，不是网页管理地址；不能含路径、查询参数或账号密码。反向代理必须同时转发 `/v1/agent/` 与 `/install/`。
3. 输入节点 ID（1–48 位字母、数字、短横线或下划线）、显示名称和图片并发组数，点击 **创建并生成安装命令**。节点默认启用。
4. 点击 **复制命令**，在目标 Linux VPS 的 root 终端执行。无需克隆仓库或编辑 `.env.agent`。若浏览器拒绝剪贴板权限，页面会选中命令供手动复制。
5. 安装器下载与主控代码匹配的最小 Agent 源码包、校验 SHA-256、生成配置、构建 Docker 镜像并启动单个 Agent 容器。回到网页点击刷新，确认节点在线。

安装器可在具备相应 apt 软件源的 Debian/Ubuntu 上安装 curl、Python 3、Docker 和 Compose v2；其他发行版需预先安装这些依赖。若系统软件源缺少 Compose v2，安装器会明确停止，先配置 Docker/Compose 后重新生成命令。构建镜像需要 VPS 能访问镜像源与 Python 包源。安装器不会安装 Telegram Bot 或 Komga，也不会开放附属机入站端口。

### 安装命令与密钥

- 命令包含 **30 分钟内单次有效**的安装凭据。主控只保存安装凭据和长期节点 Token 的哈希；未使用的命令不能从历史记录重新取回，需要重新生成。
- 重新生成命令立即作废同节点上一条未使用命令，但不影响当前 Agent；实际执行并兑换凭据时才轮换节点密钥、撤销旧租约，当前任务按既有重试规则回队。
- 命令兑换成功但后续下载/构建失败时，凭据已使用，需在网页重新生成。安装期间不要同时在另一台 VPS 使用相同节点 ID。
- 安装命令只显示在当前页面内存中，不保存到浏览器存储；退出登录时清空。请勿分享命令或将其写入公共日志。
- 更改主控 HTTPS 地址会使全部未使用安装命令失效。已安装节点不会自动更换地址，需逐台重新安装；旧域名应保留到迁移完成。

### 安装目录与日常维护

每个节点安装在 `/opt/komga-plus-agent/<节点ID>`；`.env.agent` 保存节点配置（权限 600），`compose.json` 保存容器配置，`releases/<SHA256>` 保存对应源码。目录权限 700，Agent 在容器内以非 root 用户运行。缓存使用独立 Docker 命名卷，图片上传后清理。

以节点 `vps-1` 为例，在附属 VPS 的 root 终端查看日志或停止容器：

```bash
cd /opt/komga-plus-agent/vps-1
docker compose -p "$(cat project-name)" -f compose.json logs --tail 100 -f agent
# 停止（网页删除节点不会远程删除此容器）
docker compose -p "$(cat project-name)" -f compose.json stop agent
```

主控升级后，可逐台在网页生成安装命令并执行，更新匹配版本的 Agent。原先用 `compose.agent.yaml` 手动部署的节点仍可连接；切换到网页安装方式前，先停止旧 Compose Agent，避免同一 ID 同时运行两个容器。旧 `.env.agent` 中的 Token 在重新安装后失效。

### 节点操作

- **编辑**：修改显示名称和图片并发，新的并发值在 Agent 领取下一章节时生效；旧版本 Agent 需重新安装才能接收动态并发配置。
- **停用 / 启用**：停用后立即拒绝认证和旧租约回传，运行中的任务按原重试及退避规则回队，保持原下载用户、聊天和优先级；启用后允许原 Agent 重新连接。
- **删除**：有运行任务时禁止删除，先停用，待回收后刷新再删除。删除使旧凭据永久失效；目标 VPS 上的容器需自行停止。
- **状态**：显示待安装、在线、离线、已停用，以及当前漫画、当前章节接收页数和最近通信时间。页面手动刷新，不会修改下载归属。

默认并发 2 组，范围 1–16；默认容器限制 1 GiB 内存、2 CPU。提高并发前，应同步评估内存、临时磁盘及主控接收带宽。Agent 在开始章节前保留 `AGENT_MIN_FREE_SPACE_MB` 并预留并发组的图片容量；主控和 Agent 都限制单页大小与像素数。

同一节点 ID 只能由一个 Agent 进程使用。Agent 重启产生新进程标识，旧进程的在线期尚未结束时，新进程可能要等待约一个租约周期（默认 90 秒）才能接入。

## 状态与故障恢复

- 网页“节点管理”显示每台节点的在线状态、当前漫画和当前章节已接收页数。点击刷新更新；不显示 Token。
- Telegram 管理员 `/status` 显示在线节点数和执行中漫画数量。任务进度也包含节点 ID。
- 所有节点离线时，新任务保留在原队列，不消耗重试次数；开启远程模式后主控不自行下载图片。
- 执行中节点失联，租约到期后原任务进入重试；采用现有 `RETRY_MAX_ATTEMPTS` 与退避规则。重新领取时可转交其他空闲节点，已收到且校验有效的图片会复用。
- `DISTRIBUTED_LEASE_SECONDS` 默认 90 秒，范围 30–600；Agent 最多每 15 秒续约。`DISTRIBUTED_JOB_TIMEOUT` 为**单个章节**的硬性下载/回传时限，默认 1800 秒，范围 60–86400，即使持续心跳也不会无限占用。
- 取消任务后，主控撤销该任务租约，拒绝旧回传；Agent 在下一次心跳或上传时停止下载。已发布章节按既有行为保留。
- 主控重启后，任务、用户归属及下载进度文件仍在主控持久化目录；内存租约失效，启动时恢复队列并重新分配，节点配置也保存在 schema 9 数据库中。
- 重复上传同一页不重复计费或覆盖已接收页；已过期、错误节点及已取消的租约无法发布图片。
- 日志中的 `agent_book_assigned` 可用于定位漫画任务与节点，`agent_download_error` 用于定位附属机故障；Token 不传入下载 SDK 子进程。

主控必须接收所有图片并存储完整漫画库；增加 VPS 不会消除主控带宽、磁盘和 Komga 入库的瓶颈。

## 验收清单

1. 两台不同 ID 的 Agent 上线，网页显示 2 个在线节点。
2. 提交三本不同漫画，确认两台各处理一本，第三本排队；多章节同一本始终在同一节点上完成。
3. 普通/白名单队列混合提交，确认空闲节点优先取白名单任务，重复漫画仍被拒绝。
4. 暂停其中一台 Agent，等待租约到期，确认任务重试并可在另一台恢复；旧租约上传被拒绝。
5. 下载中取消任务，确认另一节点不会再次领取正在取消的同一任务，附属机停止处理。
6. 重启主控，确认已收到图片被复用、任务完成后仅通知一次、CBZ 在主控 Komga 库可见。
7. 用错误 Token、过期租约及篡改图片校验和请求 API，确认分别被拒绝。

开发测试：`python -m unittest discover -s tests -p test_distributed.py -v` 和 `python -m unittest discover -s tests -p test_nodes.py -v`。节点测试覆盖单次凭据、过期、密钥轮换、启停删除、数据库升级、安装包及配置生成。包含双节点整本下载、章节固定节点、图片并发、白名单优先、掉线、取消、重启复用及认证/回传校验；HTTP 测试需安装 `aiohttp`。真实多 VPS、JM、Telegram 和 Komga 联调需要在部署环境完成。

## 关闭远程模式

设置主控 `.env` 中 `DISTRIBUTED_ENABLED=false` 后，在原部署目录使用原 Compose 配置重建 `app`。任务恢复为原本机下载方式。附属 VPS 可使用上方安装目录中的停止命令停止 Agent（旧手动部署仍使用原 `compose.agent.yaml` 命令）；无需删除主控数据库或漫画库。
