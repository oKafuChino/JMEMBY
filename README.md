# JMEMBY

Telegram 管理的漫画下载服务：提交禁漫漫画 ID → 确认下载 → 生成 CBZ → Komga 自动扫描 → Telegram 通知。

内置 [JMComic-Crawler-Python](https://github.com/hect0x7/JMComic-Crawler-Python) SDK 适配器和 Komga REST 适配器，无需另写下载脚本。支持白名单、持久化队列、去重、取消、重启恢复、磁盘阈值、失败重试及 Docker Compose 部署。

**验证状态：适配代码和离线契约测试已完成；由于开发环境网络受限，尚未核实上游当前源码，也未运行真实 JMComic / Komga / Telegram 或 VPS 验收。当前为待实测版本，不承诺开箱即用。**

## 一键安装

适用于 Linux VPS。脚本优先复用现有 Docker；缺失时通过 Debian/Ubuntu 的 apt 软件源安装 Docker 和 Compose v2。其他发行版请提前安装 Docker Engine、Compose v2、Python 3。

GitHub 仓库：[oKafuChino/JMEMBY](https://github.com/oKafuChino/JMEMBY)。在已安装 Git 的 VPS 交互终端执行以下命令，克隆项目并启动安装：

```bash
git clone https://github.com/oKafuChino/JMEMBY.git && cd JMEMBY && bash install.sh
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

普通用户先在私聊发送 `/register` 注册，成功后可提交下载、查看自己的任务和阅读入口。`/me` 查看自己的用户 ID 与状态。注册默认开放，开关和注册资料保存在 SQLite，重启不会丢失。

管理员继续使用 `.env` 中的 `TELEGRAM_ALLOWED_USER_IDS`，无需注册：

| 命令 | 功能 |
| --- | --- |
| `/admin` | 查看总注册人数、正常/禁用人数、管理员人数和注册开关 |
| `/users [页码]` | 每页 10 名注册用户，显示数字 ID、用户名和状态 |
| `/registration on` / `/registration off` | 开放或关闭新注册，不影响已有用户 |
| `/ban 用户ID` | 禁用用户，使未确认按钮失效并请求取消未完成任务 |
| `/unban 用户ID` | 恢复使用权限，不自动恢复已取消任务 |
| `/disk` | 管理员查看存储状态 |

注册人数不包含当前配置的管理员；被禁用用户仍计入总人数。管理员身份仅由部署配置决定，不能在聊天中自行提升。注册只提供 Bot 使用权限，不创建或管理 Komga 阅读账户，阅读仍由 Komga 自身鉴权控制。

升级启动时自动将数据库 schema 1 迁移到 2，保留原任务与漫画记录。升级前备份数据库；降级旧代码需要恢复升级前备份。

- 发送数字漫画 ID 或 `/download ID`，点击确认。
- `/queue [页码]`、`/status 任务ID`：查询队列和进度。
- `/cancel 任务ID`：取消任务，保留已发布章节。
- `/retry 任务ID`：重试失败任务，复用有效文件。
- `/library`：阅读入口；`/disk`：剩余空间。

仅私聊已注册用户和管理员可使用下载功能，命令和按钮都会重新检查权限。普通用户只能操作自己的任务；用户管理命令仅管理员可用。已保存文件和 Komga 入库分开记录；扫描接受不等于完成，只有匹配到所有文件才通知成功。

## 运维

内置 JM 下载器按章节批量执行：每批复用一个客户端和一次章节详情请求，逐页下载并校验落盘，不再为每页重复请求章节详情。自动重试复用同一任务内有效图片，只请求缺失页。批量模式采用串行图片下载，`DOWNLOAD_CONCURRENCY` 仅作用于自定义逐页适配器；每批最长运行一小时，仍支持取消和任务磁盘检查。批量运行期间显示剩余页数，结束后更新进度。

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
