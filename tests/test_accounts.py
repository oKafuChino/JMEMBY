import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from jmemby.db.store import Store
from jmemby.services.users import UserService
from jmemby.services.accounts import AccountService
from jmemby.domain.errors import JobError
from jmemby.integrations.komga.adapter import KomgaAPI, ApiError
from jmemby.bot.presentation import normalize, describe


class Bridge:
    def __init__(self):
        self.calls = []
        self.fail = False

    async def request(self, request):
        self.calls.append(request)
        await asyncio.sleep(0)
        if self.fail:
            raise JobError("NETWORK", "offline", True)
        return {"id": "remote", "email": request["email"]}


class AccountTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "app.db")
        self.users = UserService(self.store, {1})
        self.users.register(2)
        self.bridge = Bridge()
        self.accounts = AccountService(self.users, self.bridge, self.root / "account.key")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def test_creation_concurrent_idempotence_and_encrypted_password_retention(self):
        first, second = await asyncio.gather(self.accounts.ensure(2), self.accounts.ensure(2))
        self.assertEqual(first, second)
        self.assertEqual(len(self.bridge.calls), 1)
        self.assertEqual(first["email"], "tg2@jmemby.local")
        self.assertNotIn(first["password"], str(self.accounts.get(2)))
        self.assertEqual((await self.accounts.ensure(2))["password"], first['password'])
        self.assertIsNotNone(self.accounts.get(2)["password_encrypted"])
        self.assertEqual(len(self.bridge.calls), 1)
        restarted = AccountService(self.users, self.bridge, self.root / 'account.key')
        self.assertEqual(restarted.credentials(2), {
            'status': 'READY', 'email': first['email'], 'password': first['password']})
        self.assertNotIn('password_encrypted', restarted.credentials(2))

    async def test_credentials_are_per_user_and_blocked_users_cannot_decrypt(self):
        first = await self.accounts.ensure(2)
        self.users.register(3)
        second = await self.accounts.ensure(3)
        self.assertNotEqual(first['password'], second['password'])
        self.assertEqual(self.accounts.credentials(2)['password'], first['password'])
        self.assertEqual(self.accounts.credentials(3)['password'], second['password'])
        self.users.set_blocked(1, 2, True)
        with patch.object(self.accounts, '_password') as decrypt:
            for user_id in (2, 99):
                with self.assertRaises(ValueError):
                    self.accounts.credentials(user_id)
            decrypt.assert_not_called()
        self.assertIsNone(self.accounts.credentials(1))

    async def test_legacy_cleared_password_is_not_recreated_or_reset(self):
        await self.accounts.ensure(2)
        self.store.connection.execute('UPDATE komga_accounts SET password_encrypted=NULL WHERE user_id=2')
        self.assertIsNone(self.accounts.credentials(2)['password'])
        self.assertIsNone((await self.accounts.ensure(2))['password'])
        self.assertEqual(len(self.bridge.calls), 1)

    async def test_pending_credentials_are_not_disclosed(self):
        self.bridge.fail = True
        with self.assertRaises(JobError):
            await self.accounts.ensure(2)
        self.assertEqual(self.accounts.credentials(2)['status'], 'FAILED')
        self.assertIsNone(self.accounts.credentials(2)['password'])

    async def test_wrong_key_raises_safe_error_without_remote_password_reset(self):
        from cryptography.fernet import Fernet
        account = await self.accounts.ensure(2)
        self.accounts.cipher = Fernet(Fernet.generate_key())
        with self.assertRaises(JobError) as caught:
            self.accounts.credentials(2)
        self.assertEqual(caught.exception.code, 'ACCOUNT_KEY')
        self.assertNotIn(account['password'], str(caught.exception))
        with self.assertRaises(JobError):
            await self.accounts.ensure(2)
        self.assertEqual(len(self.bridge.calls), 1)

    async def test_retry_after_restart_uses_same_password(self):
        self.bridge.fail = True
        with self.assertRaises(JobError):
            await self.accounts.ensure(2)
        self.assertEqual(self.accounts.get(2)["status"], "FAILED")
        self.bridge.fail = False
        restarted = AccountService(self.users, self.bridge, self.root / "account.key")
        await restarted.ensure(2)
        self.assertEqual(self.bridge.calls[0]["password"], self.bridge.calls[1]["password"])

    async def test_banned_and_unregistered_cannot_create(self):
        self.users.set_blocked(1, 2, True)
        for user in (1, 2, 3):
            with self.assertRaises(ValueError):
                await self.accounts.ensure(user)
        self.assertEqual(self.bridge.calls, [])

    async def test_ban_during_creation_does_not_disclose_password(self):
        original = self.bridge.request
        async def request(payload):
            self.users.set_blocked(1, 2, True)
            return await original(payload)
        self.bridge.request = request
        with self.assertRaises(ValueError):
            await self.accounts.ensure(2)

    async def test_missing_encryption_key_does_not_replace_pending_credentials(self):
        await self.accounts.ensure(2)
        (self.root / "account.key").unlink()
        with self.assertRaises(RuntimeError):
            AccountService(self.users, self.bridge, self.root / "account.key")

    def test_api_creates_least_privilege_account(self):
        api = KomgaAPI("http://komga", "admin", "password", "library")
        calls = []
        user = {"id": "id", "email": "tg2@jmemby.local", "roles": ["USER"],
                "sharedAllLibraries": False, "sharedLibrariesIds": ["library"]}
        def request(method, path, query=None, payload=None):
            calls.append((method, path, payload))
            if path == "/api/v1/libraries/library":
                return {"root": "/data"}
            return [] if method == "GET" else user
        api.request = request
        self.assertEqual(api.provision_user(user["email"], "x" * 24)["id"], "id")
        payload = calls[-1][2]
        self.assertEqual(payload["roles"], [])
        self.assertEqual(payload["sharedLibraries"], {"all": False, "libraryIds": ["library"]})

    def test_api_does_not_take_over_existing_account(self):
        api = KomgaAPI("http://komga", "admin", "password", "library")
        def request(method, path, **kwargs):
            if path == "/api/v1/libraries/library":
                return {"root": "/data"}
            return [{"id": "existing", "email": "tg2@jmemby.local"}]
        api.request = request
        with patch.object(KomgaAPI, "request", side_effect=ApiError("KOMGA_AUTH")):
            with self.assertRaises(ApiError) as caught:
                api.provision_user("tg2@jmemby.local", "x" * 24)
        self.assertEqual(caught.exception.code, "KOMGA_ACCOUNT_CONFLICT")

    def test_menu_and_task_presentation(self):
        self.assertEqual(normalize("🔑 阅读账号"), "/account")
        text = describe({"snapshot": '{"title":"Example"}', "status": "COMPLETED", "progress": "", "error": None, "id": "abc"})
        self.assertIn("✅ 已完成", text)
        self.assertIn("abc", text)

    def test_api_recovers_created_user_without_password_reset(self):
        api = KomgaAPI("http://komga", "admin", "password", "library")
        user = {"id": "id", "email": "tg2@jmemby.local", "roles": ["USER"],
                "sharedAllLibraries": False, "sharedLibrariesIds": ["library"]}
        calls = []
        def request(method, path, **kwargs):
            calls.append((method, path))
            return {"root": "/data"} if path.startswith("/api/v1/") else [user]
        api.request = request
        with patch.object(KomgaAPI, "request", return_value=user):
            self.assertEqual(api.provision_user(user["email"], "x" * 24)["id"], "id")
        self.assertTrue(all(method == "GET" for method, _ in calls))

    def test_api_rejects_broader_permissions(self):
        api = KomgaAPI("http://komga", "admin", "password", "library")
        user = {"id": "id", "email": "tg2@jmemby.local", "roles": ["USER", "ADMIN"],
                "sharedAllLibraries": True, "sharedLibrariesIds": []}
        api.request = lambda method, path, **kw: {"root": "/data"} if path.startswith("/api/v1/") else [] if method == "GET" else user
        with self.assertRaises(ApiError) as caught:
            api.provision_user(user["email"], "x" * 24)
        self.assertEqual(caught.exception.code, "KOMGA_ACCOUNT_PERMISSIONS")


if __name__ == "__main__":
    unittest.main()
