import asyncio
from copy import deepcopy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from jmemby.db.store import Store
from jmemby.services.membership import MembershipDenied, MembershipService, is_member
from jmemby.services.users import UserService


class MembershipTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'db'
        self.store = Store(self.path)
        self.users = UserService(self.store, {1})
        self.users.register(2)
        self.users.grant(1, 3, True)
        self.users.register(3)
        self.chats = {
            '@channel': SimpleNamespace(id=-1001, type='channel', title='Channel', username='channel'),
            '@readers': SimpleNamespace(id=-1002, type='supergroup', title='Readers', username='readers')}
        self.bot = SimpleNamespace(id=99, get_chat=AsyncMock(side_effect=self.get_chat),
                                   get_chat_member=AsyncMock(side_effect=self.member))
        self.service = MembershipService(self.store, self.users, self.bot)
        self.payload = {'enabled': True, 'channel': {'chat_id': '@channel'}, 'group': {'chat_id': '@readers'}}

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def get_chat(self, chat_id):
        return self.chats[chat_id] if isinstance(chat_id, str) else next(c for c in self.chats.values() if c.id == chat_id)

    async def member(self, chat_id, user_id):
        return SimpleNamespace(status='administrator' if user_id == 99 else 'member')

    async def test_default_disabled_and_admin_exempt_without_network(self):
        self.assertFalse(self.service.settings(1)['enabled'])
        await self.service.require_joined(2)
        self.bot.get_chat_member.assert_not_awaited()
        await self.service.save(1, self.payload)
        self.bot.get_chat_member.reset_mock()
        await self.service.require_joined(1)
        self.bot.get_chat_member.assert_not_awaited()

    async def test_save_resolves_ids_and_survives_restart(self):
        config = await self.service.save(1, self.payload)
        self.assertEqual(config['channel'], {'chat_id': -1001, 'title': 'Channel', 'join_url': 'https://t.me/channel'})
        self.assertEqual(config['group']['chat_id'], -1002)
        self.store.close()
        self.store = Store(self.path)
        reopened = MembershipService(self.store, UserService(self.store, {1}), self.bot)
        self.assertEqual(reopened.settings(1), config)
        await reopened.require_joined(2)

    async def test_ordinary_and_white_users_must_belong_to_both_chats(self):
        await self.service.save(1, self.payload)
        for user in (2, 3, 4):
            await self.service.require_joined(user)
        async def left_group(chat_id, user_id):
            return SimpleNamespace(status='administrator' if user_id == 99 else 'left' if chat_id == -1002 else 'member')
        self.bot.get_chat_member.side_effect = left_group
        for user in (2, 3, 4):
            with self.assertRaises(MembershipDenied) as raised:
                await self.service.require_joined(user)
            self.assertEqual([t['chat_id'] for t in raised.exception.targets], [-1002])

    async def test_membership_is_rechecked_after_leaving_and_rejoining(self):
        await self.service.save(1, self.payload)
        await self.service.require_joined(2)
        async def banned(chat_id, user_id):
            return SimpleNamespace(status='administrator' if user_id == 99 else 'kicked')
        self.bot.get_chat_member.side_effect = banned
        with self.assertRaises(MembershipDenied):
            await self.service.require_joined(2)
        self.bot.get_chat_member.side_effect = self.member
        await self.service.require_joined(2)

    def test_status_semantics_include_restricted_members_only(self):
        for status in ('creator', 'administrator', 'member'):
            self.assertTrue(is_member(SimpleNamespace(status=status)))
        for status in ('left', 'kicked', 'unknown'):
            self.assertFalse(is_member(SimpleNamespace(status=status)))
        self.assertTrue(is_member(SimpleNamespace(status='restricted', is_member=True)))
        self.assertFalse(is_member(SimpleNamespace(status='restricted', is_member=False)))
        self.assertFalse(is_member(SimpleNamespace(status='restricted')))

    async def test_admin_only_and_payload_validation_without_network(self):
        for actor in (2, 3, 99):
            with self.assertRaises(ValueError):
                self.service.settings(actor)
            with self.assertRaises(ValueError):
                await self.service.save(actor, self.payload)
        for payload in ([], {}, {'enabled': 1}, {'enabled': True}):
            with self.assertRaises(ValueError):
                await self.service.save(1, payload)
        for value in (None, True, 0, 123, '-0', '-99999999999999999', '@', [], 'https://t.me/channel'):
            payload = deepcopy(self.payload)
            payload['channel']['chat_id'] = value
            with self.assertRaises(ValueError):
                await self.service.save(1, payload)
        self.bot.get_chat.assert_not_awaited()

    async def test_private_links_required_and_unsafe_urls_rejected(self):
        self.chats['@channel'].username = None
        with self.assertRaisesRegex(ValueError, '邀请链接'):
            await self.service.save(1, self.payload)
        self.payload['channel'] = {'chat_id': '-1001', 'join_url': 'https://t.me/+Abc_123-x'}
        self.assertEqual((await self.service.save(1, self.payload))['channel']['join_url'], 'https://t.me/+Abc_123-x')
        for url in ('javascript:alert(1)', 'https://evil.test/join', 'https://t.me.evil.test/join',
                    'https://t.me/channel/123', 'https://t.me/channel?x=y', 'https://t.me/@channel', []):
            self.payload['channel']['join_url'] = url
            with self.assertRaises(ValueError):
                await self.service.save(1, self.payload)

    async def test_invalid_type_or_bot_privileges_preserve_saved_config(self):
        original = await self.service.save(1, self.payload)
        self.chats['@channel'].type = 'private'
        with self.assertRaisesRegex(ValueError, '类型'):
            await self.service.save(1, self.payload)
        self.chats['@channel'].type = 'channel'
        self.bot.get_chat_member.side_effect = None
        self.bot.get_chat_member.return_value = SimpleNamespace(status='member')
        with self.assertRaisesRegex(ValueError, '管理员'):
            await self.service.save(1, self.payload)
        with self.assertRaises(MembershipDenied):
            await self.service.require_joined(2)
        self.assertEqual(self.service.settings(1), original)

    async def test_network_failures_deny_access_and_disable_works_offline(self):
        original = await self.service.save(1, self.payload)
        self.bot.get_chat.side_effect = RuntimeError('secret details')
        self.bot.get_chat_member.side_effect = RuntimeError('secret details')
        with self.assertRaises(ValueError) as raised:
            await self.service.save(1, self.payload)
        self.assertNotIn('secret', str(raised.exception))
        self.assertEqual(self.service.settings(1), original)
        with self.assertRaises(MembershipDenied):
            await self.service.require_joined(2)
        self.service.bot = None
        with self.assertRaises(MembershipDenied):
            await self.service.require_joined(2)
        disabled = await self.service.save(1, {'enabled': False})
        self.assertEqual(disabled, {**original, 'enabled': False})
        await self.service.require_joined(2)

    async def test_lookup_timeout_denies_access(self):
        await self.service.save(1, self.payload)
        timeout = asyncio.timeout
        async def stalled(*args):
            await asyncio.Event().wait()
        self.bot.get_chat_member.side_effect = stalled
        with patch('jmemby.services.membership.asyncio.timeout', side_effect=lambda seconds: timeout(.01)):
            with self.assertRaises(MembershipDenied):
                await self.service.require_joined(2)

    async def test_configuration_changes_during_lookup_do_not_allow_old_requirements(self):
        await self.service.save(1, self.payload)
        async def change_config(chat_id, user_id):
            await self.service.save(1, {'enabled': False})
            return await self.member(chat_id, user_id)
        self.bot.get_chat_member.side_effect = change_config
        with self.assertRaisesRegex(MembershipDenied, '已更新'):
            await self.service.require_joined(2)

    async def test_concurrent_disable_cannot_be_overwritten_by_earlier_enable(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed(chat_id):
            entered.set()
            await release.wait()
            return await self.get_chat(chat_id)
        self.bot.get_chat.side_effect = delayed
        enabling = asyncio.create_task(self.service.save(1, self.payload))
        await entered.wait()
        disabling = asyncio.create_task(self.service.save(1, {'enabled': False}))
        release.set()
        await asyncio.gather(enabling, disabling)
        self.assertFalse(self.service.settings(1)['enabled'])
