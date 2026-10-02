"""Isolated tests for safe inbound routing; no panel or third-party packages needed."""

import ast
import logging
import unittest
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock


ROOT = Path(__file__).resolve().parents[1]


def load_definitions(path, names, namespace):
    """Load the actual definitions while replacing unavailable external imports with fakes."""
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    selected = [node for node in tree.body if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in names]
    module = ast.Module(
        body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *selected],
        type_ignores=[],
    )
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)
    return namespace


class SessionContext:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *args):
        return False


class SafeInboundTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        class FakeAuthMode(Enum):
            SESSION = "session"
            TOKEN = "token"

        class FakeXUIError(Exception):
            pass

        class FakeNotFound(FakeXUIError):
            pass

        self.auth_mode = FakeAuthMode
        self.xui_error = FakeXUIError
        self.not_found = FakeNotFound
        self.config = SimpleNamespace(xui=SimpleNamespace(
            USERNAME="test", PASSWORD="test", TOKEN=None, INBOUND_ID=42,
            AUTH_MODE="session", API_TOKEN=None,
        ))
        self.server = SimpleNamespace(
            id=7, name="test", host="https://panel.test", online=False,
            current_clients=1, max_clients=10,
        )
        self.user = SimpleNamespace(tg_id=123, vpn_id="shop-uuid", server_id=None)
        self.server_model = SimpleNamespace(update=AsyncMock())
        self.user_model = SimpleNamespace(update=AsyncMock(return_value=self.user))
        self.inbound = lambda inbound_id, enabled=True: SimpleNamespace(
            id=inbound_id, enable=enabled, protocol="vless", remark="Shop",
            stream_settings={"security": "reality", "network": "tcp"}, raw={},
        )
        self.adapter = SimpleNamespace(
            authenticate=AsyncMock(),
            get_server_status=AsyncMock(return_value=SimpleNamespace(panel_version="v3.8.5", xray_state="running")),
            list_inbounds=AsyncMock(return_value=[self.inbound(42)]),
            get_client=AsyncMock(return_value=SimpleNamespace(
                uuid="shop-uuid", email="123", inbound_ids=(42,), limit_ip=2,
            )),
            add_client=AsyncMock(return_value=SimpleNamespace(node_pending=False)),
            update_client=AsyncMock(return_value=SimpleNamespace(node_pending=False)),
            close=AsyncMock(),
        )
        pool_ns = dict(
            dataclass=dataclass, logging=logging, logger=logging.getLogger("safe-inbound-test"),
            Server=self.server_model, User=self.user_model,
            XUIAdapter=Mock(return_value=self.adapter), XUIAuthMode=FakeAuthMode,
            TARGET_PANEL_VERSION="3.8.5", KNOWN_PROTOCOLS=frozenset({"vless"}),
        )
        load_definitions("app/bot/services/server_pool.py", {"Connection", "ServerPoolService"}, pool_ns)
        self.pool = pool_ns["ServerPoolService"](self.config, SessionContext)

    async def test_configured_inbound_exists(self):
        await self.pool._add_server(self.server)
        self.adapter.authenticate.assert_awaited_once()
        self.adapter.get_server_status.assert_awaited_once()
        self.adapter.list_inbounds.assert_awaited_once()
        self.assertTrue(self.server.online)
        self.assertIn(7, self.pool._servers)

    async def test_missing_inbound_blocks_client_add(self):
        self.adapter.list_inbounds.return_value = [self.inbound(1)]
        await self.pool._add_server(self.server)
        self.assertFalse(self.server.online)
        self.assertEqual(self.pool._servers, {})
        self.adapter.close.assert_awaited_once()
        self.pool.sync_servers = AsyncMock()
        vpn = self.make_vpn()
        self.assertFalse(await vpn.create_client(self.user, devices=2, duration=30))
        self.adapter.add_client.assert_not_awaited()
        self.user_model.update.assert_not_awaited()

    async def test_multiple_inbounds_use_exact_configured_id(self):
        self.adapter.list_inbounds.return_value = [self.inbound(99), self.inbound(42), self.inbound(1)]
        self.assertEqual((await self.pool.validate_configured_inbound(self.adapter)).id, 42)
        self.pool._servers[7] = SimpleNamespace(server=self.server, adapter=self.adapter)
        self.pool.sync_servers = AsyncMock()
        vpn = self.make_vpn()
        self.assertTrue(await vpn.create_client(self.user, devices=2, duration=30))
        self.assertEqual(self.adapter.add_client.await_args.args[0].inbound_ids, (42,))
        self.user_model.update.assert_awaited_once()

    async def test_two_valid_servers_use_existing_selection_and_assign_after_add(self):
        other_server = SimpleNamespace(
            id=8, name="other", host="https://other.test", online=True,
            current_clients=0, max_clients=10,
        )
        other_adapter = SimpleNamespace(
            list_inbounds=AsyncMock(return_value=[self.inbound(42)]),
            add_client=AsyncMock(return_value=SimpleNamespace(node_pending=False)),
        )
        self.pool._servers = {
            7: SimpleNamespace(server=self.server, adapter=self.adapter),
            8: SimpleNamespace(server=other_server, adapter=other_adapter),
        }
        self.pool.sync_servers = AsyncMock()
        selected = await self.pool.get_available_server()
        self.assertIs(selected, other_server)

        async def add_and_check_assignment(*args, **kwargs):
            self.assertIsNone(self.user.server_id)
            self.user_model.update.assert_not_awaited()
            return SimpleNamespace(node_pending=False)

        other_adapter.add_client.side_effect = add_and_check_assignment
        self.assertTrue(await self.make_vpn().create_client(self.user, 2, 30))
        other_adapter.add_client.assert_awaited_once()
        self.assertEqual(other_adapter.add_client.await_args.args[0].inbound_ids, (42,))
        self.adapter.add_client.assert_not_awaited()
        self.assertEqual(self.user.server_id, other_server.id)
        self.user_model.update.assert_awaited_once()

    async def test_login_failure_disables_server(self):
        self.adapter.authenticate.side_effect = RuntimeError("login failed")
        await self.pool._add_server(self.server)
        self.assertFalse(self.server.online)
        self.assertEqual(self.pool._servers, {})
        self.adapter.list_inbounds.assert_not_awaited()

    async def test_invalid_api_response_disables_server(self):
        self.adapter.list_inbounds.return_value = None
        await self.pool._add_server(self.server)
        self.assertFalse(self.server.online)
        self.assertEqual(self.pool._servers, {})

    async def test_inbound_api_error_blocks_add(self):
        self.adapter.list_inbounds.side_effect = RuntimeError("API unavailable")
        self.pool._servers[7] = SimpleNamespace(server=self.server, adapter=self.adapter)
        self.pool.sync_servers = AsyncMock()
        self.assertFalse(await self.make_vpn().create_client(self.user, 2, 30))
        self.adapter.add_client.assert_not_awaited()

    async def test_client_add_error_does_not_assign_server(self):
        self.adapter.add_client.side_effect = RuntimeError("add failed")
        self.pool._servers[7] = SimpleNamespace(server=self.server, adapter=self.adapter)
        self.pool.sync_servers = AsyncMock()
        self.assertFalse(await self.make_vpn().create_client(self.user, 2, 30))
        self.assertIsNone(self.user.server_id)
        self.user_model.update.assert_not_awaited()

    async def test_update_refuses_client_outside_configured_inbound(self):
        self.user.server_id = 7
        self.pool.get_connection = AsyncMock(return_value=SimpleNamespace(server=self.server, adapter=self.adapter))
        self.adapter.get_client.return_value = SimpleNamespace(
            uuid="shop-uuid", email="123", inbound_ids=(99,), limit_ip=2,
        )
        self.assertFalse(await self.make_vpn().update_client(self.user, 2, 30))
        self.adapter.update_client.assert_not_awaited()

    async def test_update_refuses_uuid_mismatch(self):
        self.user.server_id = 7
        self.pool.get_connection = AsyncMock(return_value=SimpleNamespace(server=self.server, adapter=self.adapter))
        self.adapter.get_client.return_value = SimpleNamespace(
            uuid="other-uuid", email="123", inbound_ids=(42,), limit_ip=2,
        )
        self.assertFalse(await self.make_vpn().update_client(self.user, 2, 30))
        self.adapter.update_client.assert_not_awaited()

    def make_vpn(self):
        vpn_ns = dict(
            logger=logging.getLogger("safe-inbound-test"),
            XUIClientWrite=Mock(side_effect=lambda **kw: SimpleNamespace(**kw)),
            XUIAmbiguousWriteError=self.xui_error,
            days_to_timestamp=lambda days: days * 86400000, User=self.user_model,
            XUIError=self.xui_error, XUINotFoundError=self.not_found,
            validate_client_membership=lambda client, inbound_id: (
                None if inbound_id in client.inbound_ids else (_ for _ in ()).throw(self.xui_error())
            ),
        )
        load_definitions("app/bot/services/vpn.py", {"VPNService", "VPNReadError"}, vpn_ns)
        return vpn_ns["VPNService"](self.config, SessionContext, self.pool)


class ConfigTests(unittest.TestCase):
    def test_missing_and_invalid_inbound_id(self):
        ns = load_definitions("app/config.py", {"parse_xui_inbound_id"}, {})
        parse = ns["parse_xui_inbound_id"]
        for value in (None, "", "0", "-1", "abc"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "XUI_INBOUND_ID"):
                parse(value)
        self.assertEqual(parse("42"), 42)


if __name__ == "__main__":
    unittest.main()
