# Komga Plus 多 VPS 下载

## 工作方式

**远程下载强制启用；每台附属 VPS 同时处理一本漫画，多台 VPS 并行处理不同漫画。**

主控不再提供本机下载回退。旧 `.env` 中的 `DISTRIBUTED_ENABLED=false` 也不会关闭远程模式；没有在线节点时任务保留排队，至少接入一个 Agent 后才会开始下载。

- 主控保留 Telegram Bot、管理员网页、SQLite、漫画库和 Komga。
- 附属 VPS 只运行下载 Agent，不运行 Bot、Komga 或数据库，不挂载主控目录，也不开放入站端口。
- Agent 主动通过 HTTPS 向主控请求工作、续约和回传图片。每个节点使用独立 ID 与 Token。
- 主控根据各节点的服务队列范围，按既有白名单优先、组内 FIFO 规则为在线空闲节点领取任务。正在处理的漫画不被后来提交的任务抢占。
- 一本漫画从开始下载到全部章节打包、入库确认，固定占用同一节点；期间该节点不会领取第二本。
- 一本内部的图片允许并发下载。`AGENT_CONCURRENCY` 控制这一本内部的并发下载组数，不是同时下载的漫画数量。每组最多 8 页，组内复用 SDK 客户端。
- 图片按章节回传，主控验证 SHA-256、图片格式、像素数和大小，再生成 CBZ、通知 Komga。最终文件和原始下载用户记录仍保存在主控。

例如 3 台在线 VPS 各处理 1 本，共 3 本并行；每台设置 `AGENT_CONCURRENCY=2`，表示每一本内最多 2 组同时下载，不会把一本分给多台 VPS。

此版本主控仍负责获取作品信息和章节页清单，因此主控需要能访问 JM 元数据接口。仅支持内置 JM 适配器；不能设置 `PROVIDER_COMMAND`。

## 主控配置

在已有项目目录更新代码，升级前停止 app 并备份持久化数据库（包含 WAL/SHM，或使用 SQLite backup）。本次启动会自动将数据库升级到 **schema 14**，保存节点、安装凭据哈希、节点服务队列、漫画移除记录、点赞/举报记录及任务最后执行节点；从 schema 9 或更早升级时节点默认服务两种队列，从 schema 10 升级会保留已有队列设置。本次节点记录及队列展示修复仅由主控处理，无需升级 Agent。旧程序不能直接打开升级后的数据库，回滚需恢复配套备份。

首次使用在主控 `.env` 配置：

```dotenv
WEB_ENABLED=true
WEB_PUBLIC_URL=https://admin.example.com
DOWNLOAD_AGENT_TOKENS={}
DISTRIBUTED_PORT=8081
DISTRIBUTED_LEASE_SECONDS=90
DISTRIBUTED_JOB_TIMEOUT=1800
```

允许从零节点启动，随后在网页创建节点，最多 32 个。已有 `DOWNLOAD_AGENT_TOKENS` 节点首次启动会导入数据库，之后该变量仅用于补充从未导入过的 ID，不能覆盖已有节点密钥；网页删除的节点也不会在重启时被该变量恢复。新增、编辑、启停和删除节点均无需重启主控。

主控默认 Compose 已包含 Agent API 的本机端口映射，更新时执行：

```bash
docker compose up -d --build --no-deps app
```

`compose.distributed.yaml` 保留为空兼容文件，旧的双 `-f` 更新命令仍可使用。继续使用原部署目录与原 Compose 项目名（如使用过 `-p` 参数）。

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
3. 输入节点 ID（1–48 位字母、数字、短横线或下划线）、显示名称、服务队列和图片并发组数，点击 **创建并生成安装命令**。节点默认启用。
4. 点击 **复制命令**，在目标 Linux VPS 的 root 终端执行。无需克隆仓库或编辑 `.env.agent`。若浏览器拒绝剪贴板权限，页面会选中命令供手动复制。
5. 安装器下载最小 Agent 源码包，准备独立 Python 虚拟环境，确认依赖导入正常后才兑换凭据；校验主控返回的 SHA-256，生成配置并启动原生服务。回到网页点击刷新，确认节点在线。

### 为节点指定服务队列

在网页 **节点管理 → 创建节点 / 编辑 → 服务队列** 中选择：

| 选项 | 领取规则 |
| --- | --- |
| 两者（白名单优先） | 默认选项；先领取白名单队列，再领取普通队列，各队列内按原入队顺序处理 |
| 仅普通队列 | 只领取普通队列，白名单任务不会分配给此节点 |
| 仅白名单队列 | 只领取白名单队列，普通任务不会分配给此节点 |

例如：节点 A 专用于普通队列，节点 B 专用于白名单队列，节点 C 服务两者。队列范围是严格限制，指定队列无可领取任务时节点空闲；如果某个队列没有可用节点，任务继续等待，不消耗重试次数。

设置保存在主控数据库中，主控重启后仍然有效；升级前已有节点、环境变量导入节点和新节点默认服务两者。保存后在节点领取下一本漫画时生效，正在下载的漫画继续在原节点完成，无需重启主控、重装 Agent 或轮换密钥。

队列归属取任务当前优先级，不重新按发起人的用户组推算。白名单用户重复提交普通队列中的漫画，仍按既有规则将原任务提升至白名单队列末尾；提升后的待执行任务只会被服务白名单队列的节点领取。全局去重、原始下载用户记录和每台 VPS 同时一本漫画的规则保持一致。

### 系统要求与轻量化设计

| VPS 系统 | 安装路径 | 服务管理 |
| --- | --- | --- |
| Debian 12 及以上 | apt + python3-venv | systemd |
| Ubuntu 24.04 及以上 | apt + python3-venv | systemd |
| Alpine 3.20 及以上 | apk + py3-virtualenv | OpenRC / supervise-daemon |

需要 Python 3.11+、可访问系统软件源和 Python 包源、正常运行相应 init 的 Linux VPS。版本不足或未运行 systemd/OpenRC 时，安装器会在兑换节点凭据前停止。安装命令使用 POSIX `sh`，Alpine 无需预装 Bash；安装器自动补齐 curl、CA 证书、Python 和虚拟环境工具。

网页安装默认采用 **原生进程 + Python 虚拟环境**，不安装 Docker、Compose、Bot、Komga 或数据库服务，也不开放附属机入站端口。只包含下载与回传所需源码及 aiohttp、Pillow、JMComic 依赖；优先保证小型 VPS 无需编译工具链，安装要求对应 Python/架构存在二进制 wheel（常见 amd64/arm64 仍需在实际镜像验证）。无可用 wheel 时安装会停止，不会静默安装编译器或更改系统 Python。

新建节点默认图片并发 **1 组**（每组最多 8 页，逐页处理），原有节点的并发保持不变。可以在网页提高至 16 组；并发越高，子进程和解码内存开销也越高。空闲轮询约每 5–6 秒一次。原生模式使用独立无登录权限用户；日志每文件最多 2 MiB，保留当前文件加 2 份轮转文件。图片上传后清理，重启时清理自有残留临时目录。

### 安装命令与密钥

- 命令包含 **30 分钟内单次有效**的安装凭据。主控只保存安装凭据和长期节点 Token 的哈希；未使用的命令不能从历史记录重新取回，需要重新生成。
- 重新生成命令立即作废同节点上一条未使用命令，但不影响当前 Agent；实际执行并兑换凭据时才轮换节点密钥、撤销旧租约，当前任务按既有重试规则回队。
- 命令兑换成功但后续配置/服务启动失败时，凭据已使用，需在网页重新生成。安装期间不要同时在另一台 VPS 使用相同节点 ID。
- 安装命令只显示在当前页面内存中，不保存到浏览器存储；退出登录时清空。请勿分享命令或将其写入公共日志。
- 更改主控 HTTPS 地址会使全部未使用安装命令失效。已安装节点不会自动更换地址，需逐台重新安装；旧域名应保留到迁移完成。

### 安装目录与日常维护

每个节点安装在 `/opt/komga-plus-agent/<节点ID>`；`releases/<SHA256>` 保存源码和独立虚拟环境，`current` 指向当前版本。`.env.agent` 权限 600，仅服务用户及 root 可读。软件目录归 root 所有，服务用户不可修改源码。运行状态和临时图片放在 `/var/lib/komga-plus-agent/<服务名>`，权限 700。

以节点 `vps-1` 为例，在附属 VPS 的 root 终端执行：

```sh
cd /opt/komga-plus-agent/vps-1
agent_service=$(cat service-name)
tail -n 100 -f "/var/lib/komga-plus-agent/$agent_service/agent.log"
# Debian / Ubuntu
systemctl status "$agent_service"
systemctl restart "$agent_service"
systemctl disable --now "$agent_service"  # 永久停用
# Alpine（与上面 systemd 命令二选一）
rc-service "$agent_service" status
rc-service "$agent_service" restart
rc-service "$agent_service" stop
rc-update del "$agent_service" default    # 取消开机启动
```

Agent 收到 SIGTERM 后会取消下载、清理 SDK 子进程再退出。系统服务自动开机启动并在异常退出后重启。网页停用只是拒绝节点连接，永久停止请使用系统服务命令；网页删除节点不会自动卸载 VPS 上的服务。

主控升级后，在网页重新生成并执行安装命令即可更新 Agent。安装器先准备依赖，再轮换凭据、停止旧服务并切换版本；准备依赖失败不会撤销旧节点密钥。旧源码/虚拟环境保留，确认新服务正常后可自行清理不再使用的版本，不要删除 `current` 指向的目录。

**旧 Docker 节点迁移**：若同节点目录中存在旧网页安装生成的 `compose.json` 和 `project-name`，安装器只停止该项目的 Agent，并将配置保存为 `compose.docker-backup.json`，再启动原生服务，不删除 Docker、其他容器或缓存卷。旧手工 `compose.agent.yaml` 部署不在此路径下，请先自行停止旧 Agent。原有容器仍兼容主控；同一节点 ID 不可同时运行容器与原生服务。

### 节点操作

- **测试连通性**：点击节点操作栏的按钮，主控发出单次随机探测，Agent 通过下一次任务轮询或下载心跳收到后回传确认。仅收到该节点、该进程的有效确认才显示成功，不会把历史在线状态当作测试成功。最长等待 25 秒；显示的毫秒数包含轮询等待，不是纯网络 RTT。结果和时间保留在主控内存中，重启后清空。
- 测试不创建下载任务、不访问 JM、也不改变当前漫画或队列；附属 VPS 不需要开放入站端口。下载中可以测试，同一节点同一时刻只允许一个测试。尚未安装或已停用的节点需先安装/启用。测试过程中停用、删除或重装节点会使测试失效。
- 超时仅表示未收到有效探测确认：检查 Agent 服务、网络、HTTPS 地址和凭据。**旧版 Agent 可能显示在线但测试超时**，请在网页重新生成安装命令并更新 Agent。此功能不验证 JM 数据源可访问性、下载速度或 Komga 入库。
- **编辑**：修改显示名称、服务队列和图片并发。队列范围从领取下一本漫画起生效，当前漫画继续完成；新的并发值在 Agent 领取下一章节时生效；旧版本 Agent 需重新安装才能接收动态并发配置。
- **停用 / 启用**：停用后立即拒绝认证和旧租约回传，运行中的任务按原重试及退避规则回队，保持原下载用户、聊天和优先级；启用后允许原 Agent 重新连接。
- **删除**：有运行任务时禁止删除，先停用，待回收后刷新再删除。删除使旧凭据永久失效；目标 VPS 上的服务需自行停止。
- **状态**：显示待安装、在线、离线、已停用，以及当前漫画、当前章节接收页数和最近通信时间。页面手动刷新，不会修改下载归属。

新节点默认并发 1 组，范围 1–16；原生模式不预设固定 CPU/内存硬限制，图片实际解码内存取决于尺寸。提高并发前，应同步评估内存、临时磁盘及主控接收带宽。Agent 在开始章节前保留 `AGENT_MIN_FREE_SPACE_MB` 并预留并发组的图片容量；主控和 Agent 都限制单页大小与像素数。

同一节点 ID 只能由一个 Agent 进程使用。Agent 重启产生新进程标识，旧进程的在线期尚未结束时，新进程可能要等待约一个租约周期（默认 90 秒）才能接入。

## 状态与故障恢复

### 原生 Agent 安装后一直离线、没有连接日志

旧安装器使用 `umask 077`，首次创建 `/var/lib/komga-plus-agent` 时可能将这个共享父目录设为 root 所有的 700。即使节点自己的子目录已经属于服务用户，该用户仍无法穿过父目录；systemd 可能报 `200/CHDIR`，OpenRC 可能报工作目录访问失败，Agent 尚未发出网络请求就退出。

本版安装器将共享父目录设为 755，各节点子目录仍为服务用户所有的 700，配置文件仍为 600；启动服务前还会以实际服务用户检查工作目录、配置读取、依赖导入、日志和锁文件访问。没有关闭 TLS 校验或放宽认证。

已安装节点可先在附属 VPS 的 root 终端修复，无需重新安装或轮换凭据。将 `vps-1` 替换为实际节点 ID：

```sh
chmod 755 /var/lib/komga-plus-agent
cd /opt/komga-plus-agent/vps-1
agent_service=$(cat service-name)
# Debian / Ubuntu
systemctl restart "$agent_service"
# Alpine 使用下面一条，代替 systemctl
# rc-service "$agent_service" restart
```

不要使用递归 `chmod`，节点私有目录和配置应保持原权限。回网页刷新在线状态并测试连通性；若仍离线，查看 `systemctl status` / `rc-service status` 与节点 `agent.log`，继续排查实际 HTTP 状态、HTTPS 入口和节点启用状态。更新主控后重新生成的安装命令也包含上述修复。

### 运行状态

- 网页“节点管理”显示每台节点的在线状态、当前漫画和当前章节已接收页数。点击刷新更新；不显示 Token。
- 管理员通过 `/web` 查看节点在线状态和下载队列中的负责节点；任务进度也包含节点 ID。
- 所有节点离线时，新任务保留在原队列，不消耗重试次数；主控不会自行下载图片。
- 执行中节点失联，租约到期后原任务进入重试；采用现有 `RETRY_MAX_ATTEMPTS` 与退避规则。重新领取时可转交其他空闲节点，已收到且校验有效的图片会复用。
- `DISTRIBUTED_LEASE_SECONDS` 默认 90 秒，范围 30–600；Agent 最多每 15 秒续约。`DISTRIBUTED_JOB_TIMEOUT` 为**单个章节**的硬性下载/回传时限，默认 1800 秒，范围 60–86400，即使持续心跳也不会无限占用。
- 取消任务后，主控撤销该任务租约，拒绝旧回传；Agent 在下一次心跳或上传时停止下载。已发布章节按既有行为保留。
- 主控重启后，任务、用户归属及下载进度文件仍在主控持久化目录；内存租约失效，启动时恢复队列并重新分配，节点配置也保存在 schema 14 数据库中。
- 重复上传同一页不重复计费或覆盖已接收页；已过期、错误节点及已取消的租约无法发布图片。
- 日志中的 `agent_book_assigned` 可用于定位漫画任务与节点，`agent_download_error` 用于定位附属机故障；Token 不传入下载 SDK 子进程。

主控必须接收所有图片并存储完整漫画库；增加 VPS 不会消除主控带宽、磁盘和 Komga 入库的瓶颈。

## 验收清单

CI 的 `agent-platforms` 任务在 Debian 12、Ubuntu 24.04、Alpine 3.20 容器中安装原生依赖，验证最小源码包导入与 POSIX shell 语法。该检查不启动 init，也不会连接主控；systemd/OpenRC 启停、开机恢复以及实际下载仍需在 VPS 验证。

1. 两台不同 ID 的 Agent 上线，网页显示 2 个在线节点。
2. 提交三本不同漫画，确认两台各处理一本，第三本排队；多章节同一本始终在同一节点上完成。
3. 普通/白名单队列混合提交，确认服务两者的节点优先取白名单任务；设置两个节点分别仅服务普通、仅服务白名单，确认不跨队列领取。下载中修改范围，确认当前漫画完成后才按新范围领取；无匹配节点的任务保持排队且不增加尝试次数。重复漫画仍按全局去重与白名单提升规则处理。
4. 暂停其中一台 Agent，等待租约到期，确认任务重试并可在另一台恢复；旧租约上传被拒绝。
5. 下载中取消任务，确认另一节点不会再次领取正在取消的同一任务，附属机停止处理。
6. 重启主控，确认已收到图片被复用、Telegram 提交的任务完成后仅通知一次，网页新批量任务不通知、CBZ 在主控 Komga 库可见。
7. 用错误 Token、过期租约及篡改图片校验和请求 API，确认分别被拒绝。

开发测试：`python -m unittest discover -s tests -p test_distributed.py -v` 和 `python -m unittest discover -s tests -p test_nodes.py -v`。队列专项测试：`python -m unittest discover -s tests -p test_node_queues.py -v`，覆盖三种范围、组内 FIFO、提升任务归属、重试等待、非法配置和 schema 9→10 迁移。节点测试覆盖单次凭据、过期、密钥轮换、启停删除、数据库升级、安装包及配置生成。包含双节点整本下载、章节固定节点、图片并发、白名单优先、掉线、取消、重启复用及认证/回传校验；HTTP 测试需安装 `aiohttp`。真实多 VPS、JM、Telegram 和 Komga 联调需要在部署环境完成。

## 暂停远程下载

远程模式不可关闭或退回本机下载。需要暂停时，在网页停用所有节点或在附属机停止 Agent 服务；尚未开始的任务继续保留在队列中。旧 `DISTRIBUTED_ENABLED` 变量可从 `.env` 删除，设置为 false 也不改变上述行为。
