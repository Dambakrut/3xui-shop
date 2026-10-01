"""Isolated tests for safe inbound routing; no panel or third-party packages needed."""

import ast
import logging
import unittest
from dataclasses import dataclass
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
        self.config = SimpleNamespace(xui=SimpleNamespace(
            USERNAME="test", PASSWORD="test", TOKEN=None, INBOUND_ID=42,
        ))
        self.api = SimpleNamespace(
            login=AsyncMock(),
            inbound=SimpleNamespace(get_list=AsyncMock()),
            client=SimpleNamespace(add=AsyncMock(), get_by_email=AsyncMock(), update=AsyncMock()),
        )
        self.server = SimpleNamespace(
            id=7, name="test", host="https://panel.test", online=False,
            current_clients=1, max_clients=10,
        )
        self.user = SimpleNamespace(tg_id=123, vpn_id="shop-uuid", server_id=None)
        self.server_model = SimpleNamespace(update=AsyncMock())
        self.user_model = SimpleNamespace(update=AsyncMock(return_value=self.user))
        pool_ns = dict(
            dataclass=dataclass, logging=logging, logger=logging.getLogger("safe-inbound-test"),
            AsyncApi=Mock(return_value=self.api), Server=self.server_model, User=self.user_model,
        )
        load_definitions("app/bot/services/server_pool.py", {"Connection", "ServerPoolService"}, pool_ns)
        self.pool = pool_ns["ServerPoolService"](self.config, SessionContext)

    async def test_configured_inbound_exists(self):
        self.api.inbound.get_list.return_value = [SimpleNamespace(id=42)]
        await self.pool._add_server(self.server)
        self.api.login.assert_awaited_once()
        self.api.inbound.get_list.assert_awaited_once()
        self.assertTrue(self.server.online)
        self.assertIn(7, self.pool._servers)

    async def test_missing_inbound_blocks_client_add(self):
        self.api.inbound.get_list.return_value = [SimpleNamespace(id=1)]
        await self.pool._add_server(self.server)
        self.assertFalse(self.server.online)
        self.assertEqual(self.pool._servers, {})
        self.pool.sync_servers = AsyncMock()
        vpn = self.make_vpn()
        self.assertFalse(await vpn.create_client(self.user, devices=2, duration=30))
        self.api.client.add.assert_not_awaited()
        self.user_model.update.assert_not_awaited()

    async def test_multiple_inbounds_use_exact_configured_id(self):
        self.api.inbound.get_list.return_value = [SimpleNamespace(id=99), SimpleNamespace(id=42), SimpleNamespace(id=1)]
        self.assertTrue(await self.pool.validate_configured_inbound(self.api))
        self.pool._servers[7] = SimpleNamespace(server=self.server, api=self.api)
        self.pool.sync_servers = AsyncMock()
        vpn = self.make_vpn()
        self.assertTrue(await vpn.create_client(self.user, devices=2, duration=30))
        self.assertEqual(self.api.client.add.await_args.kwargs["inbound_id"], 42)
        self.user_model.update.assert_awaited_once()

    async def test_two_valid_servers_use_existing_selection_and_assign_after_add(self):
        other_server = SimpleNamespace(
            id=8, name="other", host="https://other.test", online=True,
            current_clients=0, max_clients=10,
        )
        other_api = SimpleNamespace(
            inbound=SimpleNamespace(get_list=AsyncMock(return_value=[SimpleNamespace(id=42)])),
            client=SimpleNamespace(add=AsyncMock()),
        )
        self.pool._servers = {
            7: SimpleNamespace(server=self.server, api=self.api),
            8: SimpleNamespace(server=other_server, api=other_api),
        }
        self.pool.sync_servers = AsyncMock()
        selected = await self.pool.get_available_server()
        self.assertIs(selected, other_server)

        async def add_and_check_assignment(**kwargs):
            self.assertIsNone(self.user.server_id)
            self.user_model.update.assert_not_awaited()

        other_api.client.add.side_effect = add_and_check_assignment
        self.assertTrue(await self.make_vpn().create_client(self.user, 2, 30))
        other_api.client.add.assert_awaited_once()
        self.assertEqual(other_api.client.add.await_args.kwargs["inbound_id"], 42)
        self.api.client.add.assert_not_awaited()
        self.assertEqual(self.user.server_id, other_server.id)
        self.user_model.update.assert_awaited_once()

    async def test_login_failure_disables_server(self):
        self.api.login.side_effect = RuntimeError("login failed")
        await self.pool._add_server(self.server)
        self.assertFalse(self.server.online)
        self.assertEqual(self.pool._servers, {})
        self.api.inbound.get_list.assert_not_awaited()

    async def test_invalid_api_response_disables_server(self):
        self.api.inbound.get_list.return_value = None
        await self.pool._add_server(self.server)
        self.assertFalse(self.server.online)
        self.assertEqual(self.pool._servers, {})

    async def test_inbound_api_error_blocks_add(self):
        self.api.inbound.get_list.side_effect = RuntimeError("API unavailable")
        self.pool._servers[7] = SimpleNamespace(server=self.server, api=self.api)
        self.pool.sync_servers = AsyncMock()
        self.assertFalse(await self.make_vpn().create_client(self.user, 2, 30))
        self.api.client.add.assert_not_awaited()

    async def test_client_add_error_does_not_assign_server(self):
        self.api.inbound.get_list.return_value = [SimpleNamespace(id=42)]
        self.api.client.add.side_effect = RuntimeError("add failed")
        self.pool._servers[7] = SimpleNamespace(server=self.server, api=self.api)
        self.pool.sync_servers = AsyncMock()
        self.assertFalse(await self.make_vpn().create_client(self.user, 2, 30))
        self.assertIsNone(self.user.server_id)
        self.user_model.update.assert_not_awaited()

    async def test_update_refuses_client_outside_configured_inbound(self):
        self.user.server_id = 7
        self.pool.get_connection = AsyncMock(return_value=SimpleNamespace(server=self.server, api=self.api))
        self.api.client.get_by_email.return_value = SimpleNamespace(id="shop-uuid", email="123")
        self.api.inbound.get_list.return_value = [
            SimpleNamespace(id=42, settings=SimpleNamespace(clients=[])),
            SimpleNamespace(id=99, settings=SimpleNamespace(clients=[SimpleNamespace(id="shop-uuid", email="123", limit_ip=2)])),
        ]
        self.assertFalse(await self.make_vpn().update_client(self.user, 2, 30))
        self.api.client.update.assert_not_awaited()

    async def test_update_refuses_uuid_mismatch(self):
        self.user.server_id = 7
        self.pool.get_connection = AsyncMock(return_value=SimpleNamespace(server=self.server, api=self.api))
        self.api.client.get_by_email.return_value = SimpleNamespace(id="other-uuid", email="123")
        self.assertFalse(await self.make_vpn().update_client(self.user, 2, 30))
        self.api.inbound.get_list.assert_not_awaited()
        self.api.client.update.assert_not_awaited()

    def make_vpn(self):
        vpn_ns = dict(
            logger=logging.getLogger("safe-inbound-test"), Client=Mock(side_effect=lambda **kw: SimpleNamespace(**kw)),
            days_to_timestamp=lambda days: days * 86400000, User=self.user_model,
        )
        load_definitions("app/bot/services/vpn.py", {"VPNService"}, vpn_ns)
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
