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

在已有项目目录更新代码。为每个附属 VPS 分别生成随机 Token：

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

在主控 `.env` 增加以下配置，将占位符换成实际 Token：

```dotenv
DISTRIBUTED_ENABLED=true
DOWNLOAD_AGENT_TOKENS='{"vps-1":"<TOKEN_VPS_1>","vps-2":"<TOKEN_VPS_2>"}'
DISTRIBUTED_PORT=8081
DISTRIBUTED_LEASE_SECONDS=90
DISTRIBUTED_JOB_TIMEOUT=1800
```

Token 需为 32–256 位字母、数字、下划线或短横线，节点之间不能共用 Token。支持最多 32 个配置节点。新增、删除或更换 Token 后重建主控容器。

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
location / {
    return 404;
}
```

`client_max_body_size` 至少覆盖 `MAX_PAGE_BYTES`（默认 30 MiB）。若修改了主控宿主机 `DISTRIBUTED_PORT`，同时调整 `proxy_pass` 端口。不要将 API 转发至网页管理端口。

Agent 验证 TLS 证书，拒绝非本机明文 HTTP 和重定向。公网域名必须直达 API，不能经登录跳转页。只有 `localhost`、`127.0.0.1`、`::1` 的 HTTP 地址允许用于自行建立的 SSH 隧道；普通 Docker 网络名及公网 IP 的 HTTP 地址不被接受。

## 附属 VPS 部署

附属机安装 Docker Engine 与 Compose v2，然后在该机执行：

```bash
git clone https://github.com/oKafuChino/Komga-Plus.git
cd Komga-Plus
cp .env.agent.example .env.agent
chmod 600 .env.agent
```

编辑 `.env.agent`：

```dotenv
AGENT_MASTER_URL=https://agents.example.com
AGENT_ID=vps-1
AGENT_TOKEN=<TOKEN_VPS_1>
AGENT_CONCURRENCY=2
AGENT_OPERATION_TIMEOUT=300
AGENT_MAX_PAGE_BYTES=31457280
AGENT_MAX_PIXELS=50000000
AGENT_MIN_FREE_SPACE_MB=512
JM_API_DOMAINS=
```

第二台使用 `AGENT_ID=vps-2` 和其独立 Token。不要复制主控 `.env`，不需要 Telegram Token 或 Komga 用户名、密码。`.env.agent` 已从 Git 和 Docker 构建上下文排除。

```bash
docker compose --env-file .env.agent -f compose.agent.yaml up -d --build
docker compose --env-file .env.agent -f compose.agent.yaml logs --tail 100 -f agent
```

该 Compose 仅启动 `agent` 服务。镜像只安装 HTTP、图片和 JM 下载依赖；临时图片使用 `agent-cache` 命名卷，上传后删除，进程重启时清理残留的自有临时目录。

默认并发 2 组，范围 1–16；默认容器限制 1 GiB 内存、2 CPU。提高并发前，应同步评估内存、临时磁盘及主控接收带宽。Agent 在开始章节前保留 `AGENT_MIN_FREE_SPACE_MB` 并预留并发组的图片容量；主控和 Agent 都限制单页大小与像素数。

同一节点 ID 只能由一个 Agent 进程使用。Agent 重启产生新进程标识，旧进程的在线期尚未结束时，新进程可能要等待约一个租约周期（默认 90 秒）才能接入。

## 状态与故障恢复

- 网页“服务概览 → 下载节点”显示每台节点的在线状态、当前漫画和当前章节已接收页数。点击刷新更新；不显示 Token。
- Telegram 管理员 `/status` 显示在线节点数和执行中漫画数量。任务进度也包含节点 ID。
- 所有节点离线时，新任务保留在原队列，不消耗重试次数；开启远程模式后主控不自行下载图片。
- 执行中节点失联，租约到期后原任务进入重试；采用现有 `RETRY_MAX_ATTEMPTS` 与退避规则。重新领取时可转交其他空闲节点，已收到且校验有效的图片会复用。
- `DISTRIBUTED_LEASE_SECONDS` 默认 90 秒，范围 30–600；Agent 最多每 15 秒续约。`DISTRIBUTED_JOB_TIMEOUT` 为**单个章节**的硬性下载/回传时限，默认 1800 秒，范围 60–86400，即使持续心跳也不会无限占用。
- 取消任务后，主控撤销该任务租约，拒绝旧回传；Agent 在下一次心跳或上传时停止下载。已发布章节按既有行为保留。
- 主控重启后，任务、用户归属及下载进度文件仍在主控持久化目录；内存租约失效，启动时恢复队列并重新分配，无需数据库迁移。
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

开发测试：`python -m unittest discover -s tests -p test_distributed.py -v`。包含双节点整本下载、章节固定节点、图片并发、白名单优先、掉线、取消、重启复用及认证/回传校验；HTTP 测试需安装 `aiohttp`。真实多 VPS、JM、Telegram 和 Komga 联调需要在部署环境完成。

## 关闭远程模式

设置主控 `.env` 中 `DISTRIBUTED_ENABLED=false` 后，在原部署目录使用原 Compose 配置重建 `app`。任务恢复为原本机下载方式。附属 VPS 可执行 `docker compose --env-file .env.agent -f compose.agent.yaml stop agent` 停止 Agent；无需删除主控数据库或漫画库。
