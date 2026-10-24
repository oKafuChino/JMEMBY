"""Durable account creation with encrypted credentials for private self-service."""
import asyncio
import os
import secrets
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from jmemby.domain.errors import JobError


class AccountService:
    def __init__(self, users, bridge, key_path):
        self.users, self.store, self.bridge = users, users.store, bridge
        path = Path(key_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            pending = self.store.connection.execute("SELECT 1 FROM komga_accounts WHERE password_encrypted IS NOT NULL LIMIT 1").fetchone()
            if pending:
                raise RuntimeError("account.key missing; restore it from backup before provisioning")
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(Fernet.generate_key())
                    stream.flush()
                    os.fsync(stream.fileno())
            except FileExistsError:
                pass
        if path.is_symlink():
            raise RuntimeError("account.key must not be a symlink")
        self.cipher = Fernet(path.read_bytes())
        # Bounded lock stripes prevent simultaneous registrations from creating two accounts.
        self.locks = [asyncio.Lock() for _ in range(64)]

    def get(self, user_id):
        row = self.store.connection.execute("SELECT * FROM komga_accounts WHERE user_id=?", (user_id,)).fetchone()
        return dict(row) if row else None

    def _password(self, record):
        if not record['password_encrypted']:
            return None
        try:
            return self.cipher.decrypt(record['password_encrypted'].encode()).decode()
        except InvalidToken as exc:
            raise JobError('ACCOUNT_KEY', '账号密钥不匹配，请联系管理员恢复备份') from exc

    def credentials(self, user_id):
        """Only call with the authenticated Telegram sender in a private chat."""
        self.users.require_member(user_id)
        record = self.get(user_id)
        if not record:
            return None
        return {'status': record['status'], 'email': record['email'],
                'password': self._password(record) if record['status'] == 'READY' else None}

    async def ensure(self, user_id):
        async with self.locks[user_id % len(self.locks)]:
            self.users.require_member(user_id)
            if self.users.is_admin(user_id):
                raise ValueError("管理员请使用已有 Komga 管理账户")
            record = self.get(user_id)
            if not record:
                encrypted = self.cipher.encrypt(secrets.token_urlsafe(24).encode()).decode()
                self.store.connection.execute(
                    "INSERT INTO komga_accounts(user_id,email,password_encrypted,created_at) VALUES (?,?,?,?)",
                    (user_id, f"tg{user_id}@jmemby.local", encrypted, time.time()))
                record = self.get(user_id)
            if record["status"] != "READY":
                try:
                    password = self._password(record)
                    if not password:
                        raise JobError('ACCOUNT_PASSWORD', '阅读账号缺少已保存的密码，请联系管理员处理')
                    result = await self.bridge.request({"operation": "provision_user", "email": record["email"], "password": password})
                    if not isinstance(result.get("id"), str) or not result["id"] or result.get("email") != record["email"]:
                        raise JobError("KOMGA_PROTOCOL", "账号创建响应不完整")
                    self.store.connection.execute("UPDATE komga_accounts SET remote_id=?,status='READY',error_code=NULL WHERE user_id=?",
                                                  (result["id"], user_id))
                except JobError as exc:
                    self.store.connection.execute("UPDATE komga_accounts SET status='FAILED',error_code=? WHERE user_id=?", (exc.code, user_id))
                    raise
            # A ban during the network request prevents credential disclosure.
            self.users.require_member(user_id)
            record = self.get(user_id)
            password = self._password(record)
            return {"email": record["email"], "password": password}
