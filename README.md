# Komga Plus

Komga Plus 是一个通过 Telegram Bot 和网页管理面板管理的 Komga 漫画下载服务：发送 /jm 漫画 ID → 直接入队 → 生成 CBZ → Komga 自动扫描 → Telegram 通知。

网页“频道推送”可指定入库公告频道，成功入库后由 Bot 推送漫画信息，支持持久化重试和投递记录。默认关闭，不补发历史；批量下载默认静默，可单独开启批量频道公告。详见 [频道推送配置](docs/WEB_ADMIN.md#入库频道推送)。

网页“Bot 使用限制”可配置必须同时加入的频道和群组，普通与白名单用户均需通过成员核验；项目管理员豁免。默认关闭，启用前需将 Bot 设为两处管理员。配置步骤见 [网页管理说明](docs/WEB_ADMIN.md#bot-使用限制必加入频道和群组)。

下载过程复用同一条消息：获取信息 → 确认 → 排队/下载 → 完成。成功后原消息替换为“已完成、标题、作者”和封面，不再附带任务编号或阅读链接。封面使用首章节已下载的第一页，缺失时降级为文字。失败/取消保留结果说明供排查。若 Telegram 不允许编辑，会发送结果并尝试删除旧过程消息；受 Telegram 删除期限等限制，极少数旧消息可能无法清理。用户发送的命令和主动查询结果不会删除。

管理员通过 `/web` 查看任务、节点并取消下载。`/logs` 发送最近最多 100 条脱敏日志。Bot 已移除 `/queue`、`/status`、`/cancel`、`/retry`、`/library`；当前指令及互动规则见 [Bot 指令说明](docs/BOT_COMMANDS.md)。

下载时会尝试读取原作品的普通标签、作品（`works`）及人物（`characters`），合并去重后写入各章节 CBZ 的 `ComicInfo.xml` 的 `Tags` 字段。某个字段缺失不影响其他标签及下载，不额外请求标签接口。

ComicInfo 的系列名（`Series`）和章节标题（`Title`）也会附带 `[JM作品号]`，例如 `作品名称 [JM1481162]`，供 Komga 库中显示。请在 Komga 漫画库设置中开启 ComicInfo 元数据、系列名、标题及标签导入；扫描/分析完成后生效。手动锁定的元数据需先解除相应字段锁定。旧 CBZ 不会自动补写这些字段，仅重新扫描旧文件不会生成新增标签或号码。

新建章节文件名末尾带作品号，例如 `0001 - 第一章 [章节标识] [JM1481162].cbz`，多章节统一使用作品号而非章节号。已有数据库记录的文件路径保持不变；本次更新不会批量重命名旧文件或回填旧 CBZ 的标签。

内置 [JMComic-Crawler-Python](https://github.com/hect0x7/JMComic-Crawler-Python) SDK 适配器和 Komga REST 适配器，无需另写下载脚本。支持白名单、持久化队列、去重、取消、重启恢复、磁盘阈值、失败重试及 Docker Compose 部署。

**验证状态：适配代码和离线契约测试已完成；由于开发环境网络受限，尚未核实上游当前源码，也未运行真实 JMComic / Komga / Telegram 或 VPS 验收。当前为待实测版本，不承诺开箱即用。**

## 项目名称与升级兼容

项目名称为 **Komga Plus**。仓库地址为 `oKafuChino/Komga-Plus`，新部署的项目目录为 `Komga-Plus`；Python 包名及命令 `jmemby`、数据库文件 `jmemby.db`、阅读账号后缀 `@jmemby.local` 和 CBZ 内部标识 `jmemby.json` 保持兼容。现有部署无需重建账号或迁移数据，请继续使用原项目目录和配置。

## 多 VPS 下载

下载统一由远程 Agent 执行（强制启用，无本机回退）：**每台 VPS 同时下载一本漫画，多台 VPS 并行处理不同漫画**；一本内部可配置图片下载并发。附属机只运行 Agent，通过 HTTPS 主动领取工作并回传图片，主控统一完成去重、双队列调度、用户归属记录、CBZ 打包和 Komga 入库。网页侧边栏“节点管理”支持添加、编辑、启停、删除节点，为每个节点选择服务两种队列、仅普通队列或仅白名单队列，并生成单次有效的一键安装命令，附属 VPS 执行后自动配置原生轻量 Agent（Python 虚拟环境，不需要 Docker/Compose）。支持 Debian 12+ / Ubuntu 24.04+（systemd）和 Alpine 3.20+（OpenRC），新节点默认并发 1 组；至少一个节点在线才开始下载。数据库 schema 15，升级前请备份。配置及故障恢复说明见 [多 VPS 下载部署](docs/DISTRIBUTED_DOWNLOADS.md)。

## 一键安装

适用于 Linux VPS。脚本优先复用现有 Docker；缺失时通过 Debian/Ubuntu 的 apt 软件源安装 Docker 和 Compose v2。其他发行版请提前安装 Docker Engine、Compose v2、Python 3。

GitHub 仓库：[oKafuChino/Komga-Plus](https://github.com/oKafuChino/Komga-Plus)。在已安装 Git 的 VPS 交互终端执行以下命令，克隆项目并启动安装：

```bash
git clone https://github.com/oKafuChino/Komga-Plus.git && cd Komga-Plus && bash install.sh
```

如果已经克隆或上传了项目，在项目根目录执行：

```bash
bash install.sh
```

脚本将：

1. 检查或安装 Docker、Compose、Python 3。
2. 交互填写 Telegram Token、管理员数字用户 ID 和阅读入口，普通用户在 Bot 注册。Komga 默认使用 `gotson/komga:latest`，无需填写版本。
3. 创建持久化目录并启动 Komga。
4. 引导你首次创建 Komga 管理员和 `/data` 漫画库，然后填写管理员邮箱、密码及 library ID。
5. 构建 Bot 镜像，运行配置检查、SDK 导入和 Komga 连接检查，成功后启动 Bot。

首次创建 Komga 账户和漫画库需要一次网页操作。脚本不调用未经验证的账户初始化接口。Komga 默认仅监听 VPS 的 `127.0.0.1:25600`，在自己的电脑执行：

```bash
ssh -L 25600:127.0.0.1:25600 用户名@VPS地址
```

再访问 `http://localhost:25600`。library ID 在打开漫画库后的浏览器 URL 中。长期使用可自行配置 HTTPS 反向代理或 VPN，并更新 `KOMGA_PUBLIC_URL`。

脚本需交互终端；不要通过 `curl | bash` 运行。重复执行保留现有 `.env` 和漫画数据，不递归修改已有目录所有者。Compose 每次启动 Komga 时拉取所配置的镜像，默认拉取 `latest`；已有配置若指定了其他标签，会继续使用该标签，可将 `KOMGA_IMAGE` 改为 `gotson/komga:latest`。失败时先检查提示，修复后重新执行。若发行版软件源没有 Compose v2，脚本会停止并给出 Docker 官方安装文档。

## 手动安装

```bash
cp .env.example .env
# 编辑 .env，填写下方必要配置
mkdir -p data/app data/komga data/storage/library data/storage/staging
# 仅针对新建的数据目录，匹配 APP_UID / APP_GID
sudo chown -R 1000:1000 data
chmod 600 .env
docker compose up -d komga
# 完成账户和 /data 漫画库初始化，再填写邮箱、密码与 library ID
docker compose build app
docker compose run --rm app check-config
docker compose run --rm app check-integrations
docker compose up -d app
```

必要配置：

| 配置 | 用途 |
| --- | --- |
| TELEGRAM_BOT_TOKEN | BotFather 获取的 Token |
| TELEGRAM_ALLOWED_USER_IDS | 管理员数字用户 ID，逗号分隔；保留旧配置名，普通用户无需加入 |
| KOMGA_IMAGE | 可选，默认 `gotson/komga:latest`，留空也使用此默认值 |
| KOMGA_USERNAME / KOMGA_PASSWORD | 有扫描权限的 Komga 管理员账户 |
| KOMGA_LIBRARY_ID | 根目录为 `/data` 的漫画库 ID |
| KOMGA_PUBLIC_URL | 发给用户的阅读入口 |

默认 `KOMGA_BASE_URL=http://komga:25600` 是容器内部地址。`PROVIDER_COMMAND` 和 `KOMGA_COMMAND` 留空使用内置适配器，只有自定义适配才需要填写。`JM_API_DOMAINS` 可指定 SDK API 域名；`PROVIDER_DOMAINS` 是 Telegram 链接输入白名单，两者用途不同。默认可直接发送数字 ID。

密码可以包含 `$`、`#` 等字符；手动编辑 `.env` 时使用 Compose 支持的单引号格式。不要将 `.env` 上传仓库。脚本会以权限 600 写入配置，不回显 Token 或密码。

## Telegram 使用

### 邀请注册

管理员发送 `/invite_mode on` 开启邀请注册，发送 `/invite_create` 生成一次性邀请码（`/invite_create 10` 可允许 10 人使用）。把返回的 `/register 邀请码` 发给受邀用户，用户需在 Bot 私聊中发送这条命令才会注册成功。

- `/invites [页码]` 查看邀请码编号及使用次数，不显示原始邀请码。
- `/invite_revoke 编号` 撤销未用完的邀请码，已注册用户不受影响。
- `/invite_mode off` 恢复无需邀请码注册；已有用户始终无需重新注册。
- `/registration off` 是总开关，关闭后即使持有邀请码也不能新注册。

邀请模式默认关闭，配置和使用次数持久化。邀请码为高随机性字符串，数据库仅保存 SHA-256 摘要；明文仅在创建回复中展示，消息发送失败时可通过 `/invites` 找到编号并撤销后重建。注册与扣减次数在同一事务内完成，重复注册不重复扣减。

普通用户先在私聊发送 `/register` 注册，成功后可提交下载并接收进度通知。`/my` 查看用户 ID、组别、阅读账号和服务器地址。注册默认开放，开关和注册资料保存在 SQLite，重启不会丢失。

管理员继续使用 `.env` 中的 `TELEGRAM_ALLOWED_USER_IDS`，无需注册：

| 命令 | 功能 |
| --- | --- |
| `/admin` | 查看总注册人数、正常/禁用人数、管理员人数和注册开关 |
| `/users [页码]` | 每页 10 名注册用户，显示数字 ID、用户名和状态 |
| `/registration on` / `/registration off` | 开放或关闭新注册，不影响已有用户 |
| `/ban 用户ID` | 禁用用户，使未确认按钮失效并请求取消未完成任务 |
| `/unban 用户ID` | 恢复使用权限，不自动恢复已取消任务 |
| `/disk` | 管理员查看存储状态 |

注册人数不包含当前配置的管理员；被禁用用户仍计入总人数。管理员身份仅由部署配置决定，不能在聊天中自行提升。

### 自动开通 Komga 阅读账户

普通用户注册后自动创建 `tg用户ID@jmemby.local`，生成随机密码，只授予配置的漫画库阅读权限，不授予管理、下载或流媒体角色。该邮箱仅用作登录名，不是可收邮件的邮箱。管理员使用原有 Komga 管理账户。

- 注册成功后 Bot 私聊发送邮箱与初始密码；首次登录后请在 Komga 修改密码。
- 老用户点击「🔑 阅读账号」或发送 `/account` 可补开账号。
- Komga 不可用时保留 Bot 注册，显示开通失败；重试不重复消耗邀请码。
- 初始密码发送前加密暂存，成功发送后从数据库清除；此后 `/account` 仅显示邮箱。丢失密码请联系管理员在 Komga 重置。
- 如果请求结果丢失，重试会用同一密码确认远端账号归属；不会重置或接管已有同名账号。
- `/ban` 仅禁用 Bot，并不撤销已经创建的 Komga 账号权限。需停止阅读访问时，管理员应同时在 Komga 管理该账号。

新增底部菜单可直达首页、注册、我的账号、阅读账号、任务和阅读入口；管理员额外显示管理面板和用户列表。任务状态使用中文和图标提示。

升级启动时自动将数据库 schema 1–4 迁移到 5，保留原任务、用户、账号与漫画记录，新增定时计划和任务来源。升级前备份数据库；降级旧代码需要恢复升级前备份。账号加密密钥位于 `data/app/account.key`，必须与数据库一起备份；`update.sh` 备份整个 `data/app`，单独的数据库备份命令不包含该密钥。不要删除密钥，否则尚未交付的密码无法恢复。

账号管理接口根据 [Komga 官方 UserController](https://github.com/gotson/komga/blob/master/komga/src/main/kotlin/org/gotson/komga/interfaces/api/rest/UserController.kt) 和 [UserCreationDto](https://github.com/gotson/komga/blob/master/komga/src/main/kotlin/org/gotson/komga/interfaces/api/rest/dto/UserCreationDto.kt) 实现，使用 `/api/v2/users`。已完成离线测试，仍需在实际部署版本验收。

- `/jm ID或链接`：直接入队下载，无需确认；私聊裸 ID/链接仍提供预览确认。
- `/my`：用户信息、阅读账号及服务器地址；`/account`：领取或补开账号。
- `/report ID`：向管理员举报不合适内容；`/like ID`：为漫画点赞。
- `/random`：随机推荐；`/rank`：本站点赞人气前十。互动指令仅私聊可用。
- 管理员使用 `/web` 管理任务，`/disk` 查看剩余空间。

已注册用户和管理员可使用下载功能，命令和按钮都会重新检查权限。普通用户只能操作自己的任务；用户管理命令仅管理员可用。已保存文件和 Komga 入库分开记录；扫描接受不等于完成，只有匹配到所有文件才通知成功。

### 群聊下载

群聊管理员授权入口：项目配置的管理员直接回复用户本人消息发送 `/kk` 授予免邀请码注册资格，或 `/white` 授予资格并设为白名单组。用户随后私聊 `/register`；普通与白名单的下载权限相同。两个授权命令仅群聊可用，不接受手填 ID，注册总开关和封禁仍有效。详见 [注册与用户组](docs/REGISTRATION.md)。

先私聊机器人完成 `/register` 注册，再将机器人加入群聊，并允许其发送消息。在普通群和超级群使用 `/jm 漫画ID或链接`；有多个机器人时使用 `/jm@机器人用户名 漫画ID或链接` 明确指定接收者。发给其他机器人的命令会被忽略。

群聊开放直接下载指令 `/jm`，以及仅项目管理员可用的 `/kk`、`/white`。其他命令（包括 `/download`）、直接发送的 ID/链接和菜单文字均被忽略。注册、阅读账号和其他管理操作仍在私聊进行，匿名管理员或以频道身份发送的消息不受理。

`/jm` 直接入队，进度和完成结果发回原聊天。重复提交仍执行原去重与优先级提升规则，不改变原任务通知位置。管理员在网页取消任务；私聊裸 ID/链接的预览按钮和升级前的旧按钮继续校验原发起人、原聊天。

### 网页管理面板

网页“举报管理”默认列出待处理请求，支持搜索、状态筛选、忽略或移除漫画，并记录处理人和时间。Telegram 只发送新举报提醒，审核操作在网页完成。

网页“漫画列表”支持标题/来源 ID 搜索、分页和逐本移除：确认后删除已保存章节，保留下载历史并允许重新下载，同时请求 Komga 扫描。有未结束任务的漫画需先取消并等待释放。

网页“批量下载”支持每批粘贴 1–50 个漫画 ID 或允许的链接，以换行、空格或逗号分隔。确认后后台逐项入队并展示结果，复用现有去重和队列规则；下载者记录为当前管理员，新批量任务的进度、完成、失败与取消结果仅在网页查看，不发送 Telegram 推送。页面刷新可恢复批次进度，服务重启后仅已入队任务保留。详见 [批量下载说明](docs/WEB_ADMIN.md#批量下载)。

支持管理员登录、用户与任务统计、注册开关、用户封禁/解禁、双队列查看及取消任务、漫画下载来源查询。管理员私聊机器人发送 `/web` 获取一次性登录码；普通用户和白名单用户不能登录。默认关闭，需配置 `WEB_ENABLED=true`、`WEB_PUBLIC_URL` 并设置 HTTPS 反向代理或 SSH 隧道。配置步骤见 [网页管理面板部署说明](docs/WEB_ADMIN.md)。

### 下载来源追踪

管理员私聊发送 `/who 漫画ID或链接 [页码]` 查看首次提交者、首次成功下载者及任务历史（每页 5 条），包括发起用户 ID、本地用户名称、提交时间、状态和重试来源。用户 ID 为永久归属依据；用户名和显示名来自当前本地用户记录，不是提交时快照。群聊不响应 `/who`，普通用户无法查询他人下载记录。

历史任务直接支持查询，无需数据库迁移。成功下载者指首个完成下载和 Komga 入库任务的发起人；失败、取消或单纯提升队列不计为成功下载。任务详情也显示发起人 ID。外部手动导入的漫画及已删除的任务记录无法追溯，部分章节文件的实际下载贡献不单独统计。

### 队列与重复漫画识别

下载采用普通、白名单优先两条逻辑队列，共用一个下载 worker。已注册白名单用户的新任务进入优先队列，普通用户及未设置白名单组别的管理员进入普通队列。每次选择任务时优先处理已到执行时间的白名单任务，同队列按入队顺序执行，不抢占正在执行的任务。任务详情显示所属队列。

跨队列仍按来源和作品 ID 全局去重。白名单用户提交普通队列中等待执行的同 ID 漫画时，将原任务移到优先队列末尾，不创建新任务；原发起人、聊天、消息和任务权限不变。已在优先队列、正在执行、正在取消或已完成的重复任务直接拒绝。重试等待和磁盘暂停任务可提升，但不会跳过重试时间或磁盘检查。已注册用户刚被设为白名单时，其后新任务进入优先队列；旧普通任务可通过再次提交对应 ID 提升。

按来源和漫画 ID 全局去重，私聊、群聊及不同用户共用下载记录。同一漫画已下载完成，或仍在排队、下载、重试等待及入库处理中时，拒绝重复下载。ID 和链接指向同一作品时同样生效；确认入队时再次检查，避免多个预览同时确认产生重复任务。重复点击同一确认按钮仍返回原任务，不创建新任务。

失败和取消的漫画可用 `/jm ID` 重新提交；已有成功记录时仍拒绝重复下载。通过网页漫画列表移除后允许重新下载，原下载历史保留。去重依据应用数据库记录，不比对不同 ID 的图片，也不扫描外部导入的漫画。

## 运维

内置 JM 下载器按章节批量执行：每批复用一个客户端和一次章节详情请求，逐页下载并校验落盘，不再为每页重复请求章节详情。自动重试复用同一任务内有效图片，只请求缺失页。远程节点以图片组为单位执行，每组内部逐页下载；新节点默认 1 组，可在网页调整 `AGENT_CONCURRENCY`。主控不再使用 `DOWNLOAD_CONCURRENCY` 进行本机下载，也不支持自定义 `PROVIDER_COMMAND`；仍支持取消和任务磁盘检查。批量运行期间显示剩余页数，结束后更新进度。

磁盘空间阈值检查暂时默认关闭（`DISK_SPACE_CHECK_ENABLED=false`），旧 `.env` 未设置此项时也关闭。此前因阈值暂停的任务会自动重新排队。需要恢复时设置为 `true`，继续使用 `MIN_FREE_SPACE_GB` / `RESUME_FREE_SPACE_GB`。真实磁盘写满仍会让任务失败，可在释放空间后手动重试。单页和单任务体积上限不受此开关影响。

Bot 提示数据源失败时，可独立查询漫画元数据（不下载图片）：

```bash
docker compose run --rm app check-source --manga-id 你提交的数字漫画ID
docker compose logs --tail 100 app
```

检查输出中的错误代码和 `provider_error` 日志。诊断包含 SDK 版本、失败阶段及异常类型，不包含原始 SDK 响应或凭据。`JM_API_DOMAINS` 仅在确认默认 API 域名不可达后配置为可用的 API 主机名，不应填漫画网页 URL。

如果元数据检查成功，但 Bot 偶发在 `client_init` 阶段报 `ResponseUnexpectedException`，表示初始化请求返回了 SDK 不接受的响应，并不能据此判定配置错误。客户端初始化对网络/超时/异常响应最多尝试 3 次（等待 1、2 秒），父进程超时仍有效；队列任务失败后还受任务总重试次数限制。持续失败时应继续检查数据源可达性或 SDK 兼容性，而非无限重试。

如果 Komga 日志出现 `SQLITE_CANTOPEN`，可先修复服务数据目录权限：

```bash
bash repair.sh
```

该命令停止两个服务，仅修复 `data/app`、`data/komga` 的 UID/GID 和所有者读写权限，然后启动 Komga；不会删除数据库或更改漫画文件。检查 Komga 页面和日志恢复正常后，再执行 `bash install.sh`。若错误仍存在，需要检查数据库自定义路径、磁盘空间和文件系统是否只读；权限修复不能解决这些问题。

在 VPS 项目目录执行更新命令：

```bash
bash update.sh
```

脚本使用 `git pull --ff-only` 拉取当前分支代码，拉取 Komga 镜像并重建 Bot。镜像准备完成后停止两个服务，备份 `.env`、Bot 数据库和 Komga 配置到 `data/backups/时间戳/`，再启动 Komga、检查连接并启动 Bot。保留原配置与漫画文件，不重新询问 Token。默认 Komga 使用最新镜像。

本地代码有改动时会停止，不自动覆盖。首次获取更新脚本可执行 `git pull --ff-only && bash update.sh`。更新需要短暂停机；若 Komga 检查失败，Bot 保持停止，修复后重跑更新命令。备份包含凭据，应妥善保管；不自动回滚数据库，也不自动清理历史备份。

```bash
docker compose logs --tail 100 app
docker compose ps
docker compose restart app
docker compose down  # 停止服务，bind mount 中数据保留
docker compose exec app python -m jmemby.main backup --destination /app-data/backup-2026-10-10.db
```

备份目标必须不存在。Komga 数据库按停机方式备份 `data/komga`；漫画在 `data/storage/library`。升级前备份全部数据库及 `.env`；不要直接复制运行中的 SQLite 主文件而忽略 WAL。更改 `.env` 后用 `docker compose up -d --force-recreate app` 应用配置。

## 开发与测试

本轮安全和可靠性修复及已知限制见 [2026-10-17 审查记录](docs/REVIEW_2026-10-17.md)。

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
python -m pytest -q
bash -n install.sh
```

仅执行本地可运行的核心与适配器契约测试（Python 3.12 + Pillow）：

```bash
PYTHONPATH=src python -m unittest discover -s tests -p test_core.py -v
PYTHONPATH=src python -m unittest discover -s tests -p test_adapters.py -v
```

真实连接检查：`python -m jmemby.main check-integrations`；只导入 JMComic SDK 并查询 Komga 漫画库，不下载漫画。仍需通过 Bot 提交授权小样本验证数据源、图片还原和真实入库。

## 当前限制

- 外部依赖尚未安装核实或生成锁文件；Docker 镜像构建尚未实测，不是可重复发布构建。
- JM 下载调用 SDK 自带图片还原；文件大小在 SDK 下载完成后校验，尚无严格的流式字节上限。大响应可能超过临时存储预算，应控制并发并预留空间。
- Komga 使用 Basic 鉴权、library 扫描和分页 book API；不同版本响应字段差异仍需实机核实。
- SQLite 使用标准库和版本化 SQL 迁移；大章节同步打包可能暂时阻塞 Bot，临时目录尚未自动清理。

详细见 [集成说明](docs/INTEGRATION_NOTES.md)、[验收记录](docs/VALIDATION.md) 和 [部署说明](docs/DEPLOYMENT.md)。

## 升级说明

自动下载功能已于 2026-10-24 移除。现在通过发送漫画 ID 或链接、确认下载来添加任务；保留任务查询、取消、失败重试、CBZ 和 Komga 入库。

首次启动新版会升级数据库到 schema 6，取消尚未完成的旧自动任务，删除旧自动计划、草稿、确认、通知与去重/等待状态。已完成的下载记录、已保存漫画、用户与阅读账号保留；手动任务继续按原队列处理，不再受五分钟间隔限制。旧客户端缓存的菜单可通过 `/start` 刷新。

更新前请备份数据库和账号密钥，再重建 app 容器。具体步骤及回滚限制见 [部署说明](docs/DEPLOYMENT.md)，验证情况见 [功能移除记录](docs/AUTO_DOWNLOAD_REMOVAL.md)。
