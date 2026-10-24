"""Short-lived, single-use Telegram-issued login codes and server sessions."""
import hashlib
import secrets
import time


class AdminAuth:
    def __init__(self, users):
        self.users = users
        self.codes = {}
        self.sessions = {}
        self.attempts = []

    @staticmethod
    def digest(value):
        return hashlib.sha256(value.encode()).hexdigest()

    def prune(self):
        now = time.monotonic()
        self.codes = {k: v for k, v in self.codes.items() if v[1] > now}
        self.sessions = {k: v for k, v in self.sessions.items() if v['expires'] > now}
        self.attempts = [t for t in self.attempts if t > now - 60]

    def issue(self, user_id):
        self.users.require_admin(user_id)
        self.prune()
        self.codes = {k: v for k, v in self.codes.items() if v[0] != user_id}
        code = secrets.token_urlsafe(24)
        self.codes[self.digest(code)] = (user_id, time.monotonic() + 300)
        return code

    def login(self, code):
        self.prune()
        if len(self.attempts) >= 30:
            raise ValueError('登录尝试过多，请一分钟后重试')
        self.attempts.append(time.monotonic())
        record = self.codes.pop(self.digest(code.strip()), None)
        if not record:
            raise ValueError('登录码无效或已过期，请私聊机器人重新发送 /web')
        self.users.require_admin(record[0])
        token = secrets.token_urlsafe(32)
        self.sessions = {k: v for k, v in self.sessions.items() if v['user_id'] != record[0]}
        self.sessions[self.digest(token)] = {
            'user_id': record[0], 'csrf': secrets.token_urlsafe(32),
            'expires': time.monotonic() + 8 * 3600}
        return token

    def authenticate(self, token):
        self.prune()
        session = self.sessions.get(self.digest(token))
        if not session or not self.users.is_admin(session['user_id']):
            raise ValueError('请先以管理员身份登录')
        return session

    def logout(self, token):
        self.sessions.pop(self.digest(token), None)
