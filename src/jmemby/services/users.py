"""Registration and authorization based only on authenticated Telegram IDs."""
import time
import hashlib
import secrets


class UserService:
    def __init__(self, store, admin_ids):
        self.store = store
        self.admin_ids = frozenset(admin_ids)

    def is_admin(self, user_id):
        return user_id in self.admin_ids

    def get(self, user_id):
        row = self.store.connection.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        return dict(row) if row else None

    def require_admin(self, user_id):
        if not self.is_admin(user_id):
            raise ValueError("此功能仅管理员可用")

    def require_member(self, user_id):
        if self.is_admin(user_id):
            return
        user = self.get(user_id)
        if not user:
            raise ValueError("请先发送 /register 注册后使用机器人")
        if user["blocked"]:
            raise ValueError("账号已被禁用，请联系管理员")

    def registration_open(self):
        return self.store.connection.execute("SELECT value FROM settings WHERE key='registration_open'").fetchone()[0] == "1"

    def invite_required(self):
        return self.store.connection.execute("SELECT value FROM settings WHERE key='invite_required'").fetchone()[0] == "1"

    def set_invite_required(self, actor, enabled):
        self.require_admin(actor)
        self.store.connection.execute("UPDATE settings SET value=? WHERE key='invite_required'", ("1" if enabled else "0",))

    def create_invite(self, actor, max_uses=1):
        self.require_admin(actor)
        if not 1 <= max_uses <= 10000:
            raise ValueError("邀请码使用次数须为 1–10000")
        code = secrets.token_urlsafe(24)
        cursor = self.store.connection.execute(
            "INSERT INTO invites(code_hash,max_uses,created_by,created_at) VALUES (?,?,?,?)",
            (hashlib.sha256(code.encode()).hexdigest(), max_uses, actor, time.time()))
        return cursor.lastrowid, code

    def list_invites(self, actor, page=1):
        self.require_admin(actor)
        if not 1 <= page <= 1_000_000:
            raise ValueError("页码无效")
        return [dict(r) for r in self.store.connection.execute(
            "SELECT id,max_uses,used,revoked FROM invites ORDER BY id DESC LIMIT 10 OFFSET ?", ((page-1)*10,))]

    def revoke_invite(self, actor, invite_id):
        self.require_admin(actor)
        if not self.store.connection.execute("UPDATE invites SET revoked=1 WHERE id=?", (invite_id,)).rowcount:
            raise ValueError("邀请码编号不存在")

    def register(self, user_id, username="", display_name="", invite_code=""):
        if user_id <= 0:
            raise ValueError("用户 ID 无效")
        if self.is_admin(user_id):
            return "你是管理员，无需注册即可使用机器人"
        with self.store.transaction() as db:
            existing = self.get(user_id)
            if existing:
                self.require_member(user_id)
                return "你已注册，可以直接发送漫画 ID"
            if not self.registration_open():
                raise ValueError("管理员已关闭新用户注册")
            if self.invite_required():
                code = invite_code.strip()
                if not code:
                    raise ValueError("当前为邀请注册，请发送 /register 邀请码")
                if len(code) > 128:
                    raise ValueError("邀请码无效、已撤销或使用次数已耗尽")
                updated = db.execute(
                    "UPDATE invites SET used=used+1 WHERE code_hash=? AND revoked=0 AND used<max_uses",
                    (hashlib.sha256(code.encode()).hexdigest(),))
                if updated.rowcount != 1:
                    raise ValueError("邀请码无效、已撤销或使用次数已耗尽")
            db.execute("INSERT INTO users(user_id,username,display_name,registered_at) VALUES (?,?,?,?)",
                       (user_id, (username or "")[:64], (display_name or "")[:128], time.time()))
        return "注册成功！发送漫画 ID 或 /help 开始使用"

    def set_registration(self, actor, enabled):
        self.require_admin(actor)
        self.store.connection.execute("UPDATE settings SET value=? WHERE key='registration_open'", ("1" if enabled else "0",))

    def set_blocked(self, actor, target, blocked):
        self.require_admin(actor)
        if self.is_admin(target):
            raise ValueError("不能禁用管理员；管理员由部署环境配置")
        if not self.get(target):
            raise ValueError("该用户尚未注册")
        with self.store.transaction() as db:
            db.execute("UPDATE users SET blocked=? WHERE user_id=?", (int(blocked), target))
            if blocked:
                db.execute("UPDATE confirmations SET dismissed=1 WHERE user_id=? AND task_id IS NULL", (target,))
                # Persist cancellation atomically with the ban; worker reconciles on restart.
                db.execute("UPDATE tasks SET cancel_requested=1,status='CANCEL_REQUESTED' "
                           "WHERE user_id=? AND status NOT IN ('COMPLETED','FAILED','CANCELLED')", (target,))

    def statistics(self, actor):
        self.require_admin(actor)
        users = self.store.connection.execute("SELECT user_id,blocked FROM users").fetchall()
        members = [u for u in users if u["user_id"] not in self.admin_ids]
        return {"total": len(members), "blocked": sum(u["blocked"] for u in members),
                "active": sum(not u["blocked"] for u in members),
                "admins": len(self.admin_ids), "registration_open": self.registration_open(),
                "invite_required": self.invite_required()}

    def list_users(self, actor, page=1):
        self.require_admin(actor)
        if not 1 <= page <= 1_000_000:
            raise ValueError("页码无效")
        return [dict(r) for r in self.store.connection.execute(
            "SELECT * FROM users ORDER BY registered_at DESC,user_id LIMIT 10 OFFSET ?", ((page - 1) * 10,))]
