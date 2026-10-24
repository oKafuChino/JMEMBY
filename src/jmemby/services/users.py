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

    def grant(self, actor, target, white=False):
        self.require_admin(actor)
        if target <= 0 or self.is_admin(target):
            raise ValueError("只能授权普通用户，不能修改管理员组别")
        with self.store.transaction() as db:
            existing = self.get(target)
            if existing and existing["blocked"]:
                raise ValueError("该用户已被禁用，请先在私聊使用 /unban 解禁")
            db.execute("INSERT INTO registration_grants(user_id,user_group,granted_by,granted_at) VALUES (?,?,?,?) "
                       "ON CONFLICT(user_id) DO UPDATE SET "
                       "user_group=CASE WHEN excluded.user_group='white' THEN 'white' ELSE registration_grants.user_group END,"
                       "granted_by=excluded.granted_by,granted_at=excluded.granted_at",
                       (target, "white" if white else "normal", actor, time.time()))
            if existing and white:
                db.execute("UPDATE users SET user_group='white' WHERE user_id=?", (target,))
        return "已设为白名单组" if white else "已给予注册资格（保留已有组别）"

    def group_label(self, user_id):
        if self.is_admin(user_id):
            return "管理员"
        record = self.get(user_id) or self.store.connection.execute(
            "SELECT user_group FROM registration_grants WHERE user_id=?", (user_id,)).fetchone()
        return "白名单" if record and record["user_group"] == "white" else "普通"

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
            grant = db.execute("SELECT user_group FROM registration_grants WHERE user_id=?", (user_id,)).fetchone()
            if self.invite_required() and not grant:
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
            db.execute("INSERT INTO users(user_id,username,display_name,registered_at,user_group) VALUES (?,?,?,?,?)",
                       (user_id, (username or "")[:64], (display_name or "")[:128], time.time(),
                        grant["user_group"] if grant else "normal"))
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

    def list_users(self, actor, page=1, query=""):
        self.require_admin(actor)
        if not 1 <= page <= 1_000_000:
            raise ValueError("页码无效")
        if not isinstance(query, str) or len(query) > 200:
            raise ValueError("搜索关键词最多 200 个字符")
        query = query.strip()
        where, params = "", ()
        if query:
            def pattern(value):
                return '%' + value.replace('!', '!!').replace('%', '!%').replace('_', '!_') + '%'
            where = (" WHERE CAST(user_id AS TEXT) LIKE ? ESCAPE '!'"
                     " OR username LIKE ? ESCAPE '!' OR display_name LIKE ? ESCAPE '!'")
            username_query = query[1:] if query.startswith('@') and len(query) > 1 else query
            params = (pattern(query), pattern(username_query), pattern(query))
        return [dict(r) for r in self.store.connection.execute(
            "SELECT * FROM users" + where + " ORDER BY registered_at DESC,user_id LIMIT 10 OFFSET ?",
            (*params, (page - 1) * 10))]
