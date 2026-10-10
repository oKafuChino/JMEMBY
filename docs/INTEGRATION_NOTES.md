# 集成验证记录

状态：待外部网络和凭据验证（2026-10-10）。

## 最新实现（2026-10-10）

新增内置 `jmemby.providers.jm_adapter` 和 `jmemby.integrations.komga.adapter`。以下早期“未实现”记录保留为历史；当前不再需要用户提供外部适配器。

JMComic 采用用户指定项目 https://github.com/hect0x7/JMComic-Crawler-Python ，依赖范围暂为 `jmcomic>=2.5,<3`。调用 JmOption.construct/new_jm_client、get_album_detail、get_photo_detail、download_by_image_detail；保存稳定章节 ID/页索引并在重试时解析图片地址，SDK 负责图片还原。**这些接口依据兼容实现编写，本轮未能下载源码或安装 SDK 核实，不属于上游验证结果。** SDK 下载后执行大小检查，未实现严格流式限额。

Komga 采用 Basic 鉴权：GET /api/v1/libraries/{id} 核对 root；POST /api/v1/libraries/{id}/scan；GET /api/v1/books 传 library_id/page/size 分页。按 libraryId 与 url 完整路径匹配，支持 file URI。429 Retry-After 和网络/5xx 可重试，401/403 终止。接口及 DTO 尚需真实版本核实；7 项新测试为本地契约测试，不证明服务器兼容。

新增安装脚本，要求用户选择固定 Komga 镜像，未凭空指定已验证版本。`check-integrations` 可在 VPS 核实 SDK 导入与 library 路径；扫描/下载还需完整任务验收。

## 数据源

- 当前仓库提供 `CommandProvider` 进程协议、严格的数字 ID/域名白名单解析，不声明任何禁漫 API 端点已经可用。
- 真实禁漫客户端适配器仍未实现。主应用可通过协议取得元数据、页清单和还原后的图片，离线测试使用生成的图像。
- 未下载真实漫画内容；需使用获得授权的测试内容完成 M0 验收。

## Komga

- 尚未连接 Komga 实例，library ID、鉴权方式、扫描端点和深链接规则均待验证。
- 应在选定固定 Komga 镜像版本后，根据该版本文档填写验证日期、请求示例（脱敏）、路径映射和扫描确认方式。

## 阻塞原因

本开发环境未提供 Telegram Token、Komga 实例或数据源凭据；读取 PyPI 元数据的普通网络请求被沙箱拒绝，提权请求的自动审批服务返回 403，操作未执行。没有绕过审批。真实集成必须在恢复网络审批和具备测试凭据后单独执行并记录结果。

## 本地环境与选型差异

- Python 3.12.14、Pillow 12.3.0 可用，核心测试在此环境运行。
- 无 Docker 命令，aiogram、pytest、Ruff 未安装，因此不能验证容器或 Telegram 层运行。
- 使用标准库 sqlite3、user_version SQL 迁移、dataclass 配置及 unittest 核心测试替代原计划的 SQLAlchemy/Alembic/pydantic-settings。后续是否迁回原方案应基于实际并发和维护需求决定。
- pyproject 中版本范围尚未经过依赖解析，没有伪造锁文件或选择未经验证的 Komga 镜像版本。
