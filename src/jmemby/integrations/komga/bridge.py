"""Version-specific Komga operations supplied by a verified external adapter.

This intentionally shares the cancellable JSON process transport. There are no
guessed HTTP endpoints in the application. See PROVIDER_PROTOCOL.md.
"""
from jmemby.providers.command import CommandProvider
import os


class KomgaBridge(CommandProvider):
    def child_environment(self):
        return {key: value for key, value in os.environ.items()
                if not key.startswith(("TELEGRAM_", "JM_", "PROVIDER_"))}

    def error_message(self, code):
        return {
            "KOMGA_ACCOUNT_CONFLICT": "该登录邮箱已存在且无法确认归属，请联系管理员；不会重置原账号。",
            "KOMGA_ACCOUNT_PERMISSIONS": "阅读账号权限与预期不符，请联系管理员检查账号权限。",
            "KOMGA_AUTH": "Komga 登录失败（HTTP 401），请检查管理员邮箱和密码。",
            "KOMGA_FORBIDDEN": "Komga 拒绝访问（HTTP 403），请使用有权限的管理员账户。",
            "KOMGA_NOT_FOUND": "Komga 接口或漫画库不存在（HTTP 404），请检查 KOMGA_BASE_URL 和 KOMGA_LIBRARY_ID。",
            "KOMGA_LIBRARY_NOT_FOUND": "Komga 漫画库列表可访问，但指定库 ID 无效。请先在网页创建 /data 漫画库，再填写该库的 ID（不是库名称）。",
            "KOMGA_API_NOT_FOUND": "Komga 漫画库 API 返回 404，请将容器地址设为 http://komga:25600；若配置了服务路径前缀，需一并填写。",
            "KOMGA_ROOT_MISMATCH": "漫画库根目录不匹配，请在 Komga 中将目标库根目录设为 /data，并核对 KOMGA_LIBRARY_ROOT。",
            "KOMGA_PROTOCOL": "Komga 返回格式不符合预期，请检查服务地址及版本兼容性。",
            "KOMGA_HTTP": "Komga 返回非预期 HTTP 状态，请检查服务地址和反向代理。",
            "NETWORK": "无法连接 Komga；容器内地址应为 http://komga:25600，请确认服务已启动。",
            "SERVER_ERROR": "Komga 暂时不可用或扫描未完成，请稍后重试并查看 Komga 日志。",
            "RATE_LIMIT": "Komga 请求受限，请稍后重试。",
        }.get(code, "Komga 检查失败，请查看错误代码和容器日志。")

    async def scan(self):
        result = await self.request({"operation": "scan"})
        if result.get('ok') is not True:
            raise ValueError('invalid scan response')

    async def import_paths(self, paths):
        result = await self.request({"operation": "import", "paths": paths})
        books = result.get("books", {})
        if not isinstance(books, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in books.items()):
            raise ValueError("invalid import response")
        return books
