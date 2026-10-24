"""Pure presentation helpers; no Telegram network dependency."""
import json
from datetime import datetime, timezone

MENU = {"🏠 首页": "/start", "📝 注册": "/register", "👤 我的账号": "/my",
        "🎲 随机推荐": "/random", "🏆 人气排行": "/rank", "🔑 阅读账号": "/account",
        "⚙️ 管理面板": "/admin", "👥 用户列表": "/users",
        "🌐 网页管理": "/web", "📜 最近日志": "/logs"}
STATUS = {"QUEUED": "⏳ 排队中", "DOWNLOADING": "📥 下载中", "PACKAGING": "📦 整理中",
          "IMPORT_PENDING": "📚 正在入库", "COMPLETED": "✅ 已完成", "FAILED": "❌ 失败",
          "RETRY_WAIT": "🔄 等待重试", "PAUSED_DISK": "⏸ 空间不足",
          "CANCEL_REQUESTED": "⏳ 正在取消", "CANCELLED": "🚫 已取消"}


def normalize(text):
    return MENU.get(text.strip(), text.strip())


def profile_text(user_id, status, reading, public_url, is_admin=False):
    ready = bool(reading and reading['status'] == 'READY')
    email = reading['email'] if ready else '尚未开通，请使用 /account 领取或重试'
    password = '尚未开通'
    note = ''
    if ready:
        password = reading.get('password') or '未保存，无法回显；忘记密码请联系管理员重置'
        note = ('\n\n此处显示 Bot 开通账号时的初始密码。若已在 Komga 修改，'
                '请使用修改后的密码；Bot 不会同步该修改。') if reading.get('password') else ''
    if is_admin and not ready:
        email = '请使用已有 Komga 管理账号'
        password = '请使用已有 Komga 管理账号的密码'
    return (f"👤 我的账号\n\n用户 ID：{user_id}\nBot 状态：{status}\n"
            f"阅读账号：{email}\n阅读密码：{password}\n"
            f"服务器地址：{public_url or '尚未配置，请联系管理员'}{note}")


def describe(task):
    title = str(json.loads(task["snapshot"])["title"])[:200]
    lines = [f"📖 {title}", STATUS.get(task["status"], task["status"]), ""]
    lines.append("队列：" + ("白名单优先" if task.get("priority", 0) else "普通"))
    if task.get("user_id") is not None:
        lines.append(f"下载发起人 ID：{task['user_id']}")
    if task["progress"]:
        lines.append(task["progress"])
    if task["error"]:
        lines.append(f"提示：{task['error']}")
    lines.extend(["", f"任务编号：{task['id']}"])
    return "\n".join(lines)[:3500]


def describe_history(history):
    def identity(task):
        if not task:
            return "暂无记录"
        # IDs are durable; names come from the current local user record.
        name = " ".join((task.get("display_name") or "").split())[:40]
        username = " ".join((task.get("username") or "").split())[:32]
        return f"{task['user_id']}" + (f" · {name}" if name else "") + (f" (@{username})" if username else "")

    lines = [f"📖 {history['title'][:200]}", f"漫画 ID：{history['source_id']}",
             f"首次提交者：{identity(history['first'])}",
             f"首次成功下载者：{identity(history['completed'])}",
             "成功指下载并完成入库；姓名为本地用户记录，归属以用户 ID 为准。",
             f"任务记录：共 {history['total']} 条，第 {history['page']} 页（每页 5 条）"]
    for task in history["tasks"]:
        submitted = datetime.fromtimestamp(task["created_at"], timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        lines.extend(["", f"发起人：{identity(task)}",
                      f"状态：{STATUS.get(task['status'], task['status'])}",
                      f"提交时间：{submitted}", f"任务编号：{task['id']}"])
        if task["retry_of"]:
            lines.append(f"重试来源：{task['retry_of']}")
    if not history["tasks"]:
        lines.append("本页暂无记录")
    return "\n".join(lines)
