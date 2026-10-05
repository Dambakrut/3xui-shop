"""Runtime read integration with faked panel and database; no external requests."""

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from test_runtime import dummy_env


AVAILABLE = all(importlib.util.find_spec(name) for name in
                ("aiohttp", "py3xui", "sqlalchemy", "environs", "aiogram"))
FIXTURES = Path(__file__).parent / "fixtures" / "xui" / "v3_8_5"
UUID = "11111111-1111-4111-8111-111111111111"


def fixture(name):
    import json
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))["obj"]


class FakeSession:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *_args):
        return False


@unittest.skipUnless(AVAILABLE, "Requires the locked runtime dependencies")
class XUIReadConfigTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.env = dummy_env(directory.name)

    def load(self, changes=None):
        from app.config import load_config

        values = dict(self.env)
        values.update(changes or {})
        with patch.dict(os.environ, values, clear=True), patch("environs.Env.read_env"):
            return load_config()

    def test_session_is_default_and_legacy_token_is_not_bearer(self):
        config = self.load({"XUI_TOKEN": "legacy-login-secret", "XUI_API_TOKEN": "ignored"})
        self.assertEqual(config.xui.AUTH_MODE, "session")
        self.assertEqual(config.xui.USERNAME, "dummy")
        self.assertEqual(config.xui.PASSWORD, "dummy")
        self.assertEqual(config.xui.TOKEN, "legacy-login-secret")
        self.assertIsNone(config.xui.API_TOKEN)
        self.assertNotIn("legacy-login-secret", repr(config.xui))

    def test_session_requires_nonempty_username_and_password(self):
        for name in ("XUI_USERNAME", "XUI_PASSWORD"):
            for value in (None, "", "   "):
                with self.subTest(name=name, value=value):
                    env = dict(self.env)
                    if value is None:
                        env.pop(name)
                    else:
                        env[name] = value
                    self.env, previous = env, self.env
                    try:
                        with self.assertRaisesRegex(ValueError, name):
                            self.load()
                    finally:
                        self.env = previous

    def test_token_mode_is_explicit_and_does_not_use_legacy_token(self):
        env = dict(self.env, XUI_AUTH_MODE="token", XUI_API_TOKEN="new-api-token",
                   XUI_TOKEN="legacy-login-secret", XUI_USERNAME="", XUI_PASSWORD="")
        self.env = env
        config = self.load()
        self.assertEqual(config.xui.AUTH_MODE, "token")
        self.assertEqual(config.xui.API_TOKEN, "new-api-token")
        self.assertEqual(config.xui.TOKEN, "legacy-login-secret")
        self.assertIsNone(config.xui.USERNAME)
        self.assertIsNone(config.xui.PASSWORD)
        self.assertNotIn("new-api-token", repr(config.xui))

    def test_token_mode_requires_separate_nonempty_token(self):
        self.env["XUI_AUTH_MODE"] = "token"
        self.env["XUI_TOKEN"] = "legacy-alone"
        for value in (None, "", "   "):
            with self.subTest(value=value):
                if value is None:
                    self.env.pop("XUI_API_TOKEN", None)
                else:
                    self.env["XUI_API_TOKEN"] = value
                with self.assertRaisesRegex(ValueError, "XUI_API_TOKEN"):
                    self.load()

    def test_unknown_auth_mode_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "XUI_AUTH_MODE"):
            self.load({"XUI_AUTH_MODE": "legacy"})

    def test_explicit_subscription_base_validation(self):
        self.assertEqual(
            self.load({"XUI_SUBSCRIPTION_BASE_URL": "https://sub.example/custom/path/"})
            .xui.SUBSCRIPTION_BASE_URL, "https://sub.example/custom/path/",
        )
        for uri in ("http://sub.example/custom/", "https://sub.example/custom",
                    "https://user:pass@sub.example/custom/", "https://sub.example/custom/?q=1"):
            with self.subTest(uri=uri), self.assertRaisesRegex(ValueError, "XUI_SUBSCRIPTION_BASE_URL"):
                self.load({"XUI_SUBSCRIPTION_BASE_URL": uri})


@unittest.skipUnless(AVAILABLE, "Requires the locked runtime dependencies")
class ServerPoolReadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from app.bot.services.server_pool import ServerPoolService

        self.config = SimpleNamespace(xui=SimpleNamespace(
            USERNAME="dummy", PASSWORD="dummy", TOKEN="legacy-login-secret",
            API_TOKEN=None, AUTH_MODE="session", INBOUND_ID=42,
        ))
        self.server = SimpleNamespace(id=7, name="first", host="https://first.invalid/base",
                                      online=False, current_clients=0, max_clients=10)
        self.adapter = self.fake_adapter()
        self.pool = ServerPoolService(self.config, FakeSession)

    @staticmethod
    def fake_adapter(*, inbounds=None, panel_version="v3.8.5", state="running"):
        from app.integrations.xui import XUIInboundSummary, XUIServerStatus

        items = inbounds if inbounds is not None else [
            XUIInboundSummary(99, "trojan", True, "Existing", "in-99", {}, {}),
            XUIInboundSummary(42, "vless", True, "Shop", "in-42", {"security": "reality"}, {}),
        ]
        return SimpleNamespace(
            authenticate=AsyncMock(),
            get_server_status=AsyncMock(return_value=XUIServerStatus(
                panel_version, "26.9.30", state, {})),
            list_inbounds=AsyncMock(return_value=tuple(items)),
            close=AsyncMock(),
        )

    async def test_startup_uses_modern_auth_status_and_explicit_inbound(self):
        from app.bot.services import server_pool as module
        from app.db.models import Server

        with patch.object(module, "XUIAdapter", return_value=self.adapter) as make_adapter, \
             patch.object(Server, "get_all", new=AsyncMock(return_value=[self.server])), \
             patch.object(Server, "update", new=AsyncMock()):
            await self.pool.sync_servers()
            self.assertTrue(self.server.online)
            self.assertIs(self.pool._servers[7].adapter, self.adapter)
            self.adapter.authenticate.assert_awaited_once()
            self.adapter.get_server_status.assert_awaited_once()
            self.adapter.list_inbounds.assert_awaited_once()
            make_adapter.assert_called_once_with(
                self.server.host, auth_mode=module.XUIAuthMode.SESSION,
                username="dummy", password="dummy", token=None,
            )
            self.assertFalse(hasattr(self.pool._servers[7], "api"))
            await self.pool.close()
            self.adapter.close.assert_awaited_once()

    async def test_missing_disabled_and_unknown_inbound_fail_closed_and_close(self):
        from app.bot.services import server_pool as module
        from app.db.models import Server
        from app.integrations.xui import XUIInboundSummary

        scenarios = (
            [],
            [XUIInboundSummary(42, "vless", False, "Shop", "in-42", {}, {})],
            [XUIInboundSummary(42, "unknown", True, "Shop", "in-42", {}, {})],
        )
        for items in scenarios:
            with self.subTest(items=items):
                adapter = self.fake_adapter(inbounds=items)
                with patch.object(module, "XUIAdapter", return_value=adapter), \
                     patch.object(Server, "update", new=AsyncMock()):
                    await self.pool._add_server(self.server)
                self.assertFalse(self.server.online)
                self.assertEqual(self.pool._servers, {})
                adapter.close.assert_awaited_once()

    async def test_explicit_supported_panel_versions_accepted(self):
        from app.bot.services import server_pool as module
        from app.db.models import Server

        self.assertEqual(module.SUPPORTED_PANEL_VERSIONS, frozenset({"3.8.5", "3.9.0"}))
        for version in ("3.8.5", "3.9.0", "v3.8.5", "v3.9.0"):
            adapter = self.fake_adapter(panel_version=version)
            with self.subTest(version=version), \
                 patch.object(module, "XUIAdapter", return_value=adapter), \
                 patch.object(Server, "update", new=AsyncMock()):
                await self.pool._add_server(self.server)
                self.assertTrue(self.server.online)
                self.assertIs(self.pool._servers[7].adapter, adapter)
                adapter.list_inbounds.assert_awaited_once()
                await self.pool.close()
                adapter.close.assert_awaited_once()

    async def test_other_and_malformed_panel_versions_fail_closed(self):
        from app.bot.services import server_pool as module
        from app.db.models import Server

        for version in ("3.9.1", "3.10.0", "4.0.0", "", "garbage", "3.9", "3.9.0-beta", " 3.9.0", None):
            adapter = self.fake_adapter(panel_version=version)
            with self.subTest(version=version), \
                 patch.object(module, "XUIAdapter", return_value=adapter), \
                 patch.object(Server, "update", new=AsyncMock()), \
                 patch.object(module.logger, "error") as error_log:
                await self.pool._add_server(self.server)
                self.assertFalse(self.server.online)
                self.assertEqual(self.pool._servers, {})
                adapter.list_inbounds.assert_not_awaited()
                adapter.close.assert_awaited_once()
            error_log.assert_called_once()
            self.assertIn("unsupported panel version", error_log.call_args.args[0])
            self.assertEqual(error_log.call_args.args[-1], "3.8.5, 3.9.0")

    async def test_auth_status_version_and_xray_failures_unavailable(self):
        from app.bot.services import server_pool as module
        from app.db.models import Server

        scenarios = (
            self.fake_adapter(panel_version="v3.7.0"),
            self.fake_adapter(state="stop"),
            self.fake_adapter(),
        )
        scenarios[2].authenticate.side_effect = RuntimeError("secret should not reach logs")
        for adapter in scenarios:
            with self.subTest(adapter=adapter), \
                 patch.object(module, "XUIAdapter", return_value=adapter), \
                 patch.object(Server, "update", new=AsyncMock()):
                await self.pool._add_server(self.server)
                self.assertFalse(self.server.online)
                self.assertEqual(self.pool._servers, {})
                adapter.close.assert_awaited_once()

    async def test_token_mode_uses_separate_token(self):
        from app.bot.services import server_pool as module
        from app.db.models import Server

        self.config.xui.AUTH_MODE = "token"
        self.config.xui.API_TOKEN = "new-api-token"
        self.config.xui.USERNAME = self.config.xui.PASSWORD = None
        with patch.object(module, "XUIAdapter", return_value=self.adapter) as make_adapter, \
             patch.object(Server, "update", new=AsyncMock()):
            await self.pool._add_server(self.server)
            self.assertFalse(hasattr(self.pool._servers[7], "api"))
            self.assertEqual(make_adapter.call_args.kwargs["token"], "new-api-token")
            self.assertIsNone(make_adapter.call_args.kwargs["username"])
            await self.pool.close()

    async def test_refresh_closes_old_adapter_and_replaces_connection(self):
        from app.bot.services import server_pool as module
        from app.db.models import Server

        replacement = self.fake_adapter()
        with patch.object(module, "XUIAdapter", side_effect=[self.adapter, replacement]), \
             patch.object(Server, "update", new=AsyncMock()):
            await self.pool._add_server(self.server)
            await self.pool.refresh_server(self.server)
            self.adapter.close.assert_awaited_once()
            self.assertIs(self.pool._servers[7].adapter, replacement)
            await self.pool.close()
            replacement.close.assert_awaited_once()

    async def test_sync_removes_missing_server_and_closes_adapter(self):
        from app.db.models import Server

        self.pool._servers[7] = SimpleNamespace(server=self.server, adapter=self.adapter)
        with patch.object(Server, "get_all", new=AsyncMock(return_value=[])):
            await self.pool.sync_servers()
        self.assertEqual(self.pool._servers, {})
        self.adapter.close.assert_awaited_once()


@unittest.skipUnless(AVAILABLE, "Requires the locked runtime dependencies")
class VPNReadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from app.bot.services.vpn import VPNService
        from app.integrations.xui import XUIClient, XUIClientTraffic

        self.client = XUIClient.from_api(fixture("client"))
        self.traffic = XUIClientTraffic.from_api(fixture("traffic"))
        self.adapter = SimpleNamespace(
            get_client=AsyncMock(return_value=self.client),
            get_client_traffic=AsyncMock(return_value=self.traffic),
        )
        self.legacy = SimpleNamespace(
            client=SimpleNamespace(get_by_email=AsyncMock(), add=AsyncMock(), update=AsyncMock()),
            inbound=SimpleNamespace(get_list=AsyncMock()),
        )
        self.connection = SimpleNamespace(server=SimpleNamespace(name="local"), adapter=self.adapter)
        self.pool = SimpleNamespace(get_connection=AsyncMock(return_value=self.connection))
        self.service = VPNService(SimpleNamespace(xui=SimpleNamespace(INBOUND_ID=42)), FakeSession, self.pool)
        self.user = SimpleNamespace(tg_id=123, vpn_id=UUID, server_id=7)

    def assert_no_legacy_reads_or_writes(self):
        self.legacy.client.get_by_email.assert_not_awaited()
        self.legacy.client.add.assert_not_awaited()
        self.legacy.client.update.assert_not_awaited()
        self.legacy.inbound.get_list.assert_not_awaited()

    async def test_canonical_uuid_and_multi_inbound_membership_accepted(self):
        self.assertIs(await self.service.is_client_exists(self.user), self.client)
        self.assertEqual(await self.service.get_limit_ip(self.user, self.client), 2)
        self.assertEqual(self.client.inbound_ids, (99, 42))
        self.assert_no_legacy_reads_or_writes()

    async def test_uuid_mismatch_rejected_before_traffic_or_legacy(self):
        from app.bot.services.vpn import VPNReadError

        self.user.vpn_id = "different-uuid"
        with self.assertRaises(VPNReadError):
            await self.service.is_client_exists(self.user)
        self.assertIsNone(await self.service.get_client_data(self.user))
        self.adapter.get_client_traffic.assert_not_awaited()
        self.assert_no_legacy_reads_or_writes()

    async def test_missing_configured_membership_rejected(self):
        from app.bot.services.vpn import VPNReadError
        from dataclasses import replace

        self.adapter.get_client.return_value = replace(self.client, inbound_ids=(99,))
        with self.assertRaises(VPNReadError):
            await self.service.is_client_exists(self.user)
        self.assert_no_legacy_reads_or_writes()

    async def test_traffic_is_read_separately_numeric_id_never_becomes_uuid(self):
        data = await self.service.get_client_data(self.user)
        self.assertIsNotNone(data)
        self.assertEqual(data._traffic_used, 192)
        self.assertEqual(data._traffic_total, 1073741824)
        self.assertEqual(data._max_devices, 2)
        self.assertEqual(self.traffic.id, 14825)
        self.assertEqual(self.client.uuid, UUID)
        self.adapter.get_client.assert_awaited_once_with("123")
        self.adapter.get_client_traffic.assert_awaited_once_with("123")
        self.assert_no_legacy_reads_or_writes()

    async def test_not_found_is_absent_but_panel_error_is_not_absence(self):
        from app.bot.services.vpn import VPNReadError
        from app.integrations.xui import XUIAPIError, XUINotFoundError

        self.adapter.get_client.side_effect = XUINotFoundError("not found")
        self.assertIsNone(await self.service.is_client_exists(self.user))
        self.adapter.get_client.side_effect = XUIAPIError("panel error")
        with self.assertRaises(VPNReadError):
            await self.service.is_client_exists(self.user)
        self.assert_no_legacy_reads_or_writes()

    async def test_update_without_supported_inbounds_stays_fail_closed(self):
        from app.integrations.xui import XUIInboundSummary

        self.adapter.get_inbound = AsyncMock(return_value=XUIInboundSummary(
            42, "trojan", True, "Shop", "in-42", {}, {}))
        self.adapter.update_client = AsyncMock()
        self.assertFalse(await self.service.update_client(self.user, 2, 30))
        self.adapter.update_client.assert_not_awaited()
        self.assert_no_legacy_reads_or_writes()
