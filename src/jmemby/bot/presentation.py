"""Pure presentation helpers; no Telegram network dependency."""
import json

MENU = {"🏠 首页": "/start", "📝 注册": "/register", "👤 我的账号": "/me",
        "📚 阅读入口": "/library", "📋 我的任务": "/queue", "🔑 阅读账号": "/account",
        "⚙️ 管理面板": "/admin", "👥 用户列表": "/users",
        "🩺 服务状态": "/status", "📜 最近日志": "/logs"}
STATUS = {"QUEUED": "⏳ 排队中", "DOWNLOADING": "📥 下载中", "PACKAGING": "📦 整理中",
          "IMPORT_PENDING": "📚 正在入库", "COMPLETED": "✅ 已完成", "FAILED": "❌ 失败",
          "RETRY_WAIT": "🔄 等待重试", "PAUSED_DISK": "⏸ 空间不足",
          "CANCEL_REQUESTED": "⏳ 正在取消", "CANCELLED": "🚫 已取消"}


def normalize(text):
    return MENU.get(text.strip(), text.strip())


def describe(task):
    title = str(json.loads(task["snapshot"])["title"])[:200]
    lines = [f"📖 {title}", STATUS.get(task["status"], task["status"]), ""]
    if task["progress"]:
        lines.append(task["progress"])
    if task["error"]:
        lines.append(f"提示：{task['error']}")
    lines.extend(["", f"任务编号：{task['id']}"])
    return "\n".join(lines)[:3500]
