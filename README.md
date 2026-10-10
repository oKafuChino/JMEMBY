# JMEMBY

Telegram 管理的漫画下载服务：提交禁漫漫画 ID → 确认下载 → 生成 CBZ → Komga 自动扫描 → Telegram 通知。

内置 [JMComic-Crawler-Python](https://github.com/hect0x7/JMComic-Crawler-Python) SDK 适配器和 Komga REST 适配器，无需另写下载脚本。支持白名单、持久化队列、去重、取消、重启恢复、磁盘阈值、失败重试及 Docker Compose 部署。

**验证状态：适配代码和离线契约测试已完成；由于开发环境网络受限，尚未核实上游当前源码，也未运行真实 JMComic / Komga / Telegram 或 VPS 验收。当前为待实测版本，不承诺开箱即用。**

## 一键安装

适用于 Linux VPS。脚本优先复用现有 Docker；缺失时通过 Debian/Ubuntu 的 apt 软件源安装 Docker 和 Compose v2。其他发行版请提前安装 Docker Engine、Compose v2、Python 3。

将本项目完整目录上传或克隆到 VPS 后，在项目根目录执行：

```bash
bash install.sh
```

脚本将：

1. 检查或安装 Docker、Compose、Python 3。
2. 交互填写 Telegram Token、允许使用的数字用户 ID、Komga 固定版本镜像和阅读入口。
3. 创建持久化目录并启动 Komga。
4. 引导你首次创建 Komga 管理员和 `/data` 漫画库，然后填写管理员邮箱、密码及 library ID。
5. 构建 Bot 镜像，运行配置检查、SDK 导入和 Komga 连接检查，成功后启动 Bot。

首次创建 Komga 账户和漫画库需要一次网页操作。脚本不调用未经验证的账户初始化接口。Komga 默认仅监听 VPS 的 `127.0.0.1:25600`，在自己的电脑执行：

```bash
ssh -L 25600:127.0.0.1:25600 用户名@VPS地址
```

再访问 `http://localhost:25600`。library ID 在打开漫画库后的浏览器 URL 中。长期使用可自行配置 HTTPS 反向代理或 VPN，并更新 `KOMGA_PUBLIC_URL`。

脚本需交互终端；不要通过 `curl | bash` 运行。重复执行保留现有 `.env` 和漫画数据，不自动更新版本、不递归修改已有目录所有者。失败时先检查提示，修复后重新执行。若发行版软件源没有 Compose v2，脚本会停止并给出 Docker 官方安装文档。

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
| TELEGRAM_ALLOWED_USER_IDS | 允许使用的数字用户 ID，逗号分隔 |
| KOMGA_IMAGE | `gotson/komga:具体版本号` 或镜像 digest；需自行选定并验证 |
| KOMGA_USERNAME / KOMGA_PASSWORD | 有扫描权限的 Komga 管理员账户 |
| KOMGA_LIBRARY_ID | 根目录为 `/data` 的漫画库 ID |
| KOMGA_PUBLIC_URL | 发给用户的阅读入口 |

默认 `KOMGA_BASE_URL=http://komga:25600` 是容器内部地址。`PROVIDER_COMMAND` 和 `KOMGA_COMMAND` 留空使用内置适配器，只有自定义适配才需要填写。`JM_API_DOMAINS` 可指定 SDK API 域名；`PROVIDER_DOMAINS` 是 Telegram 链接输入白名单，两者用途不同。默认可直接发送数字 ID。

密码可以包含 `$`、`#` 等字符；手动编辑 `.env` 时使用 Compose 支持的单引号格式。不要将 `.env` 上传仓库。脚本会以权限 600 写入配置，不回显 Token 或密码。

## Telegram 使用

- 发送数字漫画 ID 或 `/download ID`，点击确认。
- `/queue [页码]`、`/status 任务ID`：查询队列和进度。
- `/cancel 任务ID`：取消任务，保留已发布章节。
- `/retry 任务ID`：重试失败任务，复用有效文件。
- `/library`：阅读入口；`/disk`：剩余空间。

仅私聊白名单用户可用。已保存文件和 Komga 入库分开记录；扫描接受不等于完成，只有匹配到所有文件才通知成功。

## 运维

```bash
docker compose logs --tail 100 app
docker compose ps
docker compose restart app
docker compose down  # 停止服务，bind mount 中数据保留
docker compose exec app python -m jmemby.main backup --destination /app-data/backup-2026-10-10.db
```

备份目标必须不存在。Komga 数据库按停机方式备份 `data/komga`；漫画在 `data/storage/library`。升级前备份全部数据库及 `.env`；不要直接复制运行中的 SQLite 主文件而忽略 WAL。更改 `.env` 后用 `docker compose up -d --force-recreate app` 应用配置。

## 开发与测试

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
