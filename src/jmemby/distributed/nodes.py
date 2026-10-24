"""Persistent node registry; only hashes of node and enrollment credentials are stored."""
import hashlib
import hmac
import ipaddress
import re
import secrets
import time
from urllib.parse import urlsplit


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def public_origin(value):
    if not isinstance(value, str) or len(value) > 300 or re.search(r"[\s'\"`$\\]", value):
        raise ValueError('请输入有效的 Agent HTTPS 地址')
    value = value.rstrip('/')
    url = urlsplit(value)
    if (url.scheme != 'https' or not url.hostname or url.username or url.password
            or url.path or url.query or url.fragment):
        raise ValueError('Agent 地址必须是 HTTPS 源地址，不含路径、账号或查询参数')
    host = url.hostname
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if (len(host) > 253 or not all(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', part)
                                       for part in host.rstrip('.').split('.'))):
            raise ValueError('请使用有效域名或 IP 地址') from None
    if url.port is not None and not 1 <= url.port <= 65535:
        raise ValueError('Agent 地址端口无效')
    return value


class NodeRegistry:
    TICKET_TTL = 1800

    def __init__(self, store, initial_tokens=None, clock=time.time):
        self.store, self.clock = store, clock
        # Tombstones prevent deleted environment nodes from returning on restart.
        with store.transaction() as db:
            count = db.execute('SELECT COUNT(*) FROM download_nodes WHERE deleted=0').fetchone()[0]
            for name, token in (initial_tokens or {}).items():
                if db.execute('SELECT 1 FROM download_nodes WHERE id=?', (name,)).fetchone():
                    continue
                if count >= 32:
                    raise ValueError('最多支持 32 个下载节点')
                db.execute('INSERT INTO download_nodes(id,label,token_hash,created_at,updated_at) VALUES (?,?,?,?,?)',
                           (name, name, digest(token), clock(), clock()))
                count += 1

    def ids(self):
        return [r[0] for r in self.store.connection.execute('SELECT id FROM download_nodes WHERE deleted=0 ORDER BY created_at,id')]

    def get(self, name):
        row = self.store.connection.execute('SELECT * FROM download_nodes WHERE id=? AND deleted=0', (name,)).fetchone()
        if not row:
            raise ValueError('节点不存在')
        return dict(row)

    def enabled(self, name):
        try:
            return bool(self.get(name)['enabled'])
        except ValueError:
            return False

    def authenticate(self, name, token):
        try:
            row = self.get(name)
            return bool(row['enabled'] and row['token_hash'] and hmac.compare_digest(row['token_hash'], digest(token)))
        except (ValueError, TypeError):
            return False

    def origin(self):
        row = self.store.connection.execute("SELECT value FROM settings WHERE key='agent_public_url'").fetchone()
        return row[0] if row else ''

    def set_origin(self, value):
        value = public_origin(value)
        with self.store.transaction() as db:
            if value != self.origin():
                db.execute('UPDATE download_nodes SET ticket_hash=NULL,ticket_expires=NULL')
            db.execute("INSERT INTO settings(key,value) VALUES ('agent_public_url',?) "
                       'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (value,))
        return value

    @staticmethod
    def validate_fields(label, concurrency):
        if not isinstance(label, str) or not label.strip() or len(label) > 64 or any(ord(c) < 32 for c in label):
            raise ValueError('节点名称需为 1–64 个可见字符')
        if type(concurrency) is not int or not 1 <= concurrency <= 16:
            raise ValueError('图片并发需为 1–16 的整数')

    def create(self, name, label, concurrency):
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,48}', name):
            raise ValueError('节点 ID 仅支持 1–48 位字母、数字、下划线或短横线')
        self.validate_fields(label, concurrency)
        with self.store.transaction() as db:
            if db.execute('SELECT 1 FROM download_nodes WHERE id=? AND deleted=0', (name,)).fetchone():
                raise ValueError('节点 ID 已存在')
            if db.execute('SELECT COUNT(*) FROM download_nodes WHERE deleted=0').fetchone()[0] >= 32:
                raise ValueError('最多支持 32 个下载节点')
            db.execute('INSERT INTO download_nodes(id,label,concurrency,created_at,updated_at) VALUES (?,?,?,?,?) '
                       "ON CONFLICT(id) DO UPDATE SET label=excluded.label,concurrency=excluded.concurrency,"
                       "token_hash='',enabled=1,deleted=0,revision=revision+1,ticket_hash=NULL,ticket_expires=NULL,"
                       'updated_at=excluded.updated_at', (name, label.strip(), concurrency, self.clock(), self.clock()))

    def update(self, name, label, concurrency):
        self.get(name)
        self.validate_fields(label, concurrency)
        self.store.connection.execute('UPDATE download_nodes SET label=?,concurrency=?,updated_at=? WHERE id=?',
                                      (label.strip(), concurrency, self.clock(), name))

    def set_enabled(self, name, enabled):
        self.get(name)
        if type(enabled) is not bool:
            raise ValueError('节点启用状态无效')
        self.store.connection.execute('UPDATE download_nodes SET enabled=?,revision=revision+1,updated_at=?, '
                                      'ticket_hash=NULL,ticket_expires=NULL WHERE id=?', (int(enabled), self.clock(), name))

    def delete(self, name):
        self.get(name)
        self.store.connection.execute("UPDATE download_nodes SET enabled=0,deleted=1,token_hash='',"
                                      'ticket_hash=NULL,ticket_expires=NULL,revision=revision+1,updated_at=? WHERE id=?',
                                      (self.clock(), name))

    def issue(self, name):
        node = self.get(name)
        if not node['enabled']:
            raise ValueError('请先启用节点')
        origin = public_origin(self.origin())
        ticket = secrets.token_urlsafe(32)
        expires = self.clock() + self.TICKET_TTL
        self.store.connection.execute('UPDATE download_nodes SET ticket_hash=?,ticket_expires=? WHERE id=?',
                                      (digest(ticket), expires, name))
        from jmemby.distributed.installer import install_command
        return {'command': install_command(origin, name, ticket), 'expires_at': expires, 'node_id': name}

    def enroll(self, name, ticket):
        origin = public_origin(self.origin())
        with self.store.transaction() as db:
            node = self.get(name)
            if (not node['enabled'] or not isinstance(ticket, str) or not node['ticket_hash']
                    or node['ticket_expires'] <= self.clock()
                    or not hmac.compare_digest(node['ticket_hash'], digest(ticket))):
                raise ValueError('安装凭据无效或已过期，请重新生成命令')
            token = secrets.token_urlsafe(32)
            db.execute('UPDATE download_nodes SET token_hash=?,revision=revision+1,ticket_hash=NULL, '
                       'ticket_expires=NULL,updated_at=? WHERE id=?', (digest(token), self.clock(), name))
        return {'AGENT_MASTER_URL': origin, 'AGENT_ID': name,
                'AGENT_TOKEN': token, 'AGENT_CONCURRENCY': node['concurrency']}

    def public(self, name):
        row = self.get(name)
        return {**{key: row[key] for key in ('id', 'label', 'enabled', 'concurrency', 'created_at', 'updated_at')},
                'registered': bool(row['token_hash'])}
