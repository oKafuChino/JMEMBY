"""Persistent Telegram join requirements shared by the Bot and admin panel."""
import asyncio
import json
import logging
import re


logger = logging.getLogger(__name__)
KEY = 'bot_join_requirements'
KINDS = {'channel': ('频道', {'channel'}), 'group': ('群组', {'group', 'supergroup'})}


class MembershipDenied(ValueError):
    def __init__(self, message, targets=()):
        super().__init__(message)
        self.targets = targets


def is_member(member):
    return member.status in {'creator', 'administrator', 'member'} or (
        member.status == 'restricted' and getattr(member, 'is_member', False) is True)


class MembershipService:
    def __init__(self, store, users, bot=None):
        self.store, self.users, self.bot = store, users, bot
        self.save_lock = asyncio.Lock()

    def config(self):
        row = self.store.connection.execute('SELECT value FROM settings WHERE key=?', (KEY,)).fetchone()
        return json.loads(row['value']) if row else {'enabled': False, 'channel': None, 'group': None}

    def settings(self, actor):
        self.users.require_admin(actor)
        return self.config()

    @staticmethod
    def target_input(value, name):
        if not isinstance(value, dict):
            raise ValueError(f'请配置{name}')
        chat_id = value.get('chat_id')
        if isinstance(chat_id, str):
            chat_id = chat_id.strip()
            if re.fullmatch(r'-[1-9][0-9]{0,15}', chat_id):
                chat_id = int(chat_id)
        if not ((type(chat_id) is int and -(2**52) < chat_id < 0) or
                (isinstance(chat_id, str) and re.fullmatch(r'@[A-Za-z][A-Za-z0-9_]{3,31}', chat_id))):
            raise ValueError(f'{name}标识须为负数 Chat ID 或 @用户名')
        url = value.get('join_url', '')
        if not isinstance(url, str) or len(url) > 256:
            raise ValueError(f'{name}加入链接无效')
        url = url.strip()
        if url and not re.fullmatch(r'https://t\.me/(?:[A-Za-z][A-Za-z0-9_]{3,31}|\+[A-Za-z0-9_-]+|joinchat/[A-Za-z0-9_-]+)', url):
            raise ValueError(f'{name}加入链接须为 https://t.me/用户名 或邀请链接')
        return chat_id, url

    async def save(self, actor, data):
        self.users.require_admin(actor)
        if not isinstance(data, dict) or type(data.get('enabled')) is not bool:
            raise ValueError('请提供有效的限制开关')
        # Serialize saves so an older validation cannot overwrite a newer disable.
        async with self.save_lock:
            if not data['enabled']:
                config = {**self.config(), 'enabled': False}
            else:
                if self.bot is None:
                    raise ValueError('Bot 连接不可用，无法验证加入要求')
                inputs = {key: self.target_input(data.get(key), name) for key, (name, _) in KINDS.items()}
                config = {'enabled': True}
                try:
                    async with asyncio.timeout(15):
                        for key, (name, types) in KINDS.items():
                            chat_id, url = inputs[key]
                            chat = await self.bot.get_chat(chat_id)
                            if chat.type not in types:
                                raise ValueError(f'{name}类型不匹配，请检查 Chat ID')
                            own = await self.bot.get_chat_member(chat.id, self.bot.id)
                            if own.status not in {'creator', 'administrator'}:
                                raise ValueError(f'请先将 Bot 设为该{name}的管理员')
                            username = getattr(chat, 'username', None)
                            if not url and username:
                                url = f'https://t.me/{username}'
                            if not url:
                                raise ValueError(f'私有{name}必须提供有效的加入邀请链接')
                            config[key] = {'chat_id': chat.id, 'title': (chat.title or name)[:128], 'join_url': url}
                except ValueError:
                    raise
                except Exception as exc:
                    logger.warning('membership_config_failed type=%s', type(exc).__name__)
                    raise ValueError('无法验证频道或群组，请检查 Chat ID、Bot 管理员权限和网络；原配置保持不变') from None
                if config['channel']['chat_id'] == config['group']['chat_id']:
                    raise ValueError('频道与群组不能是同一个聊天')
            self.users.require_admin(actor)
            self.store.connection.execute(
                'INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                (KEY, json.dumps(config, ensure_ascii=False)))
            return config

    async def require_joined(self, user_id):
        if self.users.is_admin(user_id):
            return
        config = self.config()
        if not config['enabled']:
            return
        targets = [config[key] for key in KINDS]
        try:
            if self.bot is None:
                raise RuntimeError('Bot unavailable')
            async def check(target):
                own = await self.bot.get_chat_member(target['chat_id'], self.bot.id)
                if own.status not in {'creator', 'administrator'}:
                    raise RuntimeError('Bot is not an administrator')
                return is_member(await self.bot.get_chat_member(target['chat_id'], user_id))
            async with asyncio.timeout(10):
                results = await asyncio.gather(*(check(target) for target in targets), return_exceptions=True)
                if any(isinstance(result, BaseException) for result in results):
                    raise RuntimeError('Membership lookup failed')
        except Exception as exc:
            logger.warning('membership_check_failed type=%s', type(exc).__name__)
            raise MembershipDenied('暂时无法核验加入状态，请稍后重试或联系管理员。') from None
        if self.config() != config:
            raise MembershipDenied('加入要求已更新，请重新发送指令。')
        missing = [target for target, joined in zip(targets, results) if not joined]
        if missing:
            raise MembershipDenied('请先加入指定频道和群组，再重新发送指令。申请加入需审核通过后才能使用。', missing)
