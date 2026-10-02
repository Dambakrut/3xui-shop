"""Modern VPN write service tests; panel and database are fakes."""

import importlib.util
import json
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

AVAILABLE = all(importlib.util.find_spec(name) for name in ("sqlalchemy", "aiohttp"))
ROOT = Path(__file__).resolve().parents[1]
UUID = "11111111-1111-4111-8111-111111111111"


class FakeSession:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *_args):
        return False


@unittest.skipUnless(AVAILABLE, "Requires locked runtime dependencies")
class VPNWriteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from app.bot.services.vpn import VPNService
        from app.integrations.xui import XUIClient, XUIInboundSummary

        obj = json.loads((ROOT / "tests/fixtures/xui/v3_8_5/client.json").read_text())['obj']
        self.client = XUIClient.from_api(obj)
        self.inbound = XUIInboundSummary(
            42, "vless", True, "Shop", "in-42",
            {"security": "reality", "network": "tcp"}, {"disableFlow": False},
        )
        self.adapter = SimpleNamespace(
            get_client=AsyncMock(return_value=self.client),
            get_inbound=AsyncMock(return_value=self.inbound),
            add_client=AsyncMock(return_value=SimpleNamespace(node_pending=False)),
            update_client=AsyncMock(return_value=SimpleNamespace(node_pending=False)),
            get_subscription_base_url=AsyncMock(return_value="https://sub.example/custom/"),
        )
        self.server = SimpleNamespace(id=7, host="https://panel.invalid", name="panel")
        self.connection = SimpleNamespace(server=self.server, adapter=self.adapter)
        self.user = SimpleNamespace(tg_id=123, vpn_id=UUID, server_id=7)
        self.pool = SimpleNamespace(
            get_connection=AsyncMock(return_value=self.connection),
            get_provisioning_connection=AsyncMock(return_value=self.connection),
            validate_configured_inbound=AsyncMock(return_value=self.inbound),
            assign_server_to_user=AsyncMock(return_value=True),
        )
        self.config = SimpleNamespace(xui=SimpleNamespace(
            INBOUND_ID=42, SUBSCRIPTION_BASE_URL=None,
        ))
        self.vpn = VPNService(self.config, FakeSession, self.pool)

    async def test_create_uses_one_configured_inbound_and_assigns_after_add(self):
        self.user.server_id = None
        self.adapter.get_client.side_effect = None
        async def add(write):
            self.assertIsNone(self.user.server_id)
            self.pool.assign_server_to_user.assert_not_awaited()
            self.assertEqual(write.inbound_ids, (42,))
            self.assertEqual(write.uuid, UUID)
            self.assertEqual(write.sub_id, UUID)
            self.assertEqual(write.total_bytes, 0)
            self.assertEqual(write.client_payload()["flow"], "xtls-rprx-vision")
            return SimpleNamespace(node_pending=False)
        self.adapter.add_client.side_effect = add
        self.assertTrue(await self.vpn.create_client(self.user, 2, 30))
        self.pool.assign_server_to_user.assert_awaited_once()

    async def test_incompatible_protocol_prevents_create(self):
        self.pool.validate_configured_inbound.return_value = replace(self.inbound, protocol="trojan")
        self.assertFalse(await self.vpn.create_client(self.user, 2, 30))
        self.adapter.add_client.assert_not_awaited()

    async def test_incompatible_vision_transport_security_and_disabled_flow_block_create(self):
        for inbound in (
            replace(self.inbound, stream_settings={"security": "none", "network": "tcp"}),
            replace(self.inbound, stream_settings={"security": "reality", "network": "raw"}),
            replace(self.inbound, raw={"disableFlow": True}),
        ):
            self.pool.validate_configured_inbound.return_value = inbound
            with self.subTest(inbound=inbound):
                self.assertFalse(await self.vpn.create_client(self.user, 2, 30))
        self.adapter.add_client.assert_not_awaited()

    async def test_panel_add_failure_never_assigns_user_server(self):
        from app.integrations.xui import XUIAmbiguousWriteError

        self.user.server_id = None
        self.adapter.add_client.side_effect = XUIAmbiguousWriteError("Write requires review")
        with self.assertRaises(XUIAmbiguousWriteError):
            await self.vpn.create_client(self.user, 2, 30)
        self.assertIsNone(self.user.server_id)
        self.pool.assign_server_to_user.assert_not_awaited()

    def test_panel_runtime_has_no_py3xui_imports_or_legacy_mutations(self):
        import ast

        for path in (ROOT / "app").rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            tree = ast.parse(text)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    self.assertFalse((node.module or "").startswith("py3xui"), str(path))
                if isinstance(node, ast.Import):
                    self.assertFalse(any(i.name.startswith("py3xui") for i in node.names), str(path))
            for endpoint in ("/inbounds/addClient", "/inbounds/updateClient", "/inbounds/getClientTraffics"):
                self.assertNotIn(endpoint, text, str(path))

    async def test_active_renewal_preserves_quota_subid_and_memberships(self):
        import app.bot.services.vpn as module

        with patch.object(module, "get_current_timestamp", return_value=1700000000000):
            self.assertTrue(await self.vpn.update_client(self.user, 4, 30, replace_devices=True))
        current, write = self.adapter.update_client.await_args.args
        self.assertIs(current, self.client)
        self.assertEqual(write.expiry_time_ms, self.client.expiry_time_ms + 30 * 86400000)
        self.assertEqual(write.total_bytes, self.client.total_bytes)
        self.assertEqual(write.sub_id, self.client.sub_id)
        self.assertEqual(write.inbound_ids, (99, 42))
        self.assertEqual(write.limit_ip, 4)
        self.assertEqual(write.client_payload()["comment"], self.client.comment)

    async def test_expired_renewal_starts_from_now(self):
        import app.bot.services.vpn as module

        self.adapter.get_client.return_value = replace(self.client, expiry_time_ms=1600000000000)
        with patch.object(module, "get_current_timestamp", return_value=1700000000000):
            self.assertTrue(await self.vpn.update_client(self.user, 2, 30, replace_devices=True))
        write = self.adapter.update_client.await_args.args[1]
        self.assertEqual(write.expiry_time_ms, 1700000000000 + 30 * 86400000)

    async def test_zero_and_negative_expiry_fail_closed(self):
        for expiry in (0, -86400000):
            self.adapter.get_client.return_value = replace(self.client, expiry_time_ms=expiry)
            with self.subTest(expiry=expiry):
                self.assertFalse(await self.vpn.update_client(self.user, 2, 30))
        self.adapter.update_client.assert_not_awaited()

    async def test_identity_and_membership_mismatch_block_update(self):
        for client in (replace(self.client, uuid="other"),
                       replace(self.client, inbound_ids=(99,))):
            self.adapter.get_client.return_value = client
            with self.subTest(client=client):
                self.assertFalse(await self.vpn.update_client(self.user, 2, 30))
        self.adapter.update_client.assert_not_awaited()

    async def test_node_pending_and_ambiguous_write_propagate_for_payment_review(self):
        from app.integrations.xui import XUIAmbiguousWriteError

        self.adapter.update_client.return_value = SimpleNamespace(node_pending=True)
        with self.assertRaises(XUIAmbiguousWriteError):
            await self.vpn.update_client(self.user, 2, 30)
        self.adapter.update_client.side_effect = XUIAmbiguousWriteError("ambiguous")
        with self.assertRaises(XUIAmbiguousWriteError):
            await self.vpn.update_client(self.user, 2, 30)

    async def test_subscription_url_uses_panel_suburi_or_explicit_base_without_logging_key(self):
        from app.db.models import User
        import app.bot.services.vpn as module

        self.adapter.get_client.return_value = replace(self.client, sub_id=UUID)
        with patch.object(User, "get", new=AsyncMock(return_value=self.user)):
            with patch.object(module.logger, "disabled", False), self.assertLogs("app.bot.services.vpn", level="DEBUG") as logs:
                url = await self.vpn.get_key(self.user)
            self.assertEqual(url, f"https://sub.example/custom/{UUID}")
            self.assertNotIn(url, "\n".join(logs.output))
            self.config.xui.SUBSCRIPTION_BASE_URL = "https://proxy.example/base/sub/"
            url = await self.vpn.get_key(self.user)
            self.assertEqual(url, f"https://proxy.example/base/sub/{UUID}")
            self.adapter.get_subscription_base_url.assert_awaited_once()

    async def test_ambiguous_adapter_error_reaches_payment_review_and_duplicate_noop(self):
        from app.integrations.xui import XUIAmbiguousWriteError
        from test_payment_idempotency import PaymentIdempotencyTests, Status

        case = PaymentIdempotencyTests()
        case.setUp()
        self.adapter.update_client.side_effect = XUIAmbiguousWriteError("Write needs review")
        async def extend(**kwargs):
            return await self.vpn.extend_subscription(self.user, devices=2, duration=30)
        case.vpn.extend_subscription.side_effect = extend
        await case.gateway.handle_payment_succeeded("payment-1")
        self.assertEqual(case.transaction.status, Status.REVIEW_REQUIRED)
        await case.gateway.handle_payment_succeeded("payment-1")
        self.adapter.update_client.assert_awaited_once()
        case.referral.add_referrers_rewards_on_payment.assert_not_awaited()

    async def test_unknown_node_activation_requires_review(self):
        from app.integrations.xui import XUIAmbiguousWriteError, XUIWriteResult

        result = XUIWriteResult(client=self.client, node_pending=None, reconciled=True)
        self.adapter.add_client.return_value = result
        self.adapter.update_client.return_value = result
        for operation in (self.vpn.create_subscription, self.vpn.extend_subscription):
            with self.subTest(operation=operation.__name__), self.assertRaises(XUIAmbiguousWriteError):
                await operation(self.user, 2, 30)

    async def test_unknown_activation_result_enters_payment_review_without_rewards_or_retry(self):
        from app.integrations.xui import XUIWriteResult
        from test_payment_idempotency import PaymentIdempotencyTests, Status

        result = XUIWriteResult(client=self.client, node_pending=None, reconciled=True)
        for operation in ("create", "extend"):
            case = PaymentIdempotencyTests()
            case.setUp()
            case.data.is_extend = operation == "extend"
            self.adapter.add_client.reset_mock()
            self.adapter.update_client.reset_mock()
            self.adapter.add_client.return_value = result
            self.adapter.update_client.return_value = result
            async def provision(**kwargs):
                method = (self.vpn.extend_subscription if operation == "extend"
                          else self.vpn.create_subscription)
                return await method(self.user, devices=2, duration=30)
            case.vpn.create_subscription.side_effect = provision
            case.vpn.extend_subscription.side_effect = provision
            with self.subTest(operation=operation):
                await case.gateway.handle_payment_succeeded("payment-1")
                self.assertEqual(case.transaction.status, Status.REVIEW_REQUIRED)
                await case.gateway.handle_payment_succeeded("payment-1")
                write = (self.adapter.update_client if operation == "extend"
                         else self.adapter.add_client)
                write.assert_awaited_once()
                case.referral.add_referrers_rewards_on_payment.assert_not_awaited()
                case.notification.notify_purchase_success.assert_not_awaited()
                case.notification.notify_extend_success.assert_not_awaited()

    async def test_subscription_url_preserves_canonical_subid_distinct_from_uuid(self):
        from app.db.models import User
        from app.bot.services.vpn import VPNReadError

        with patch.object(User, "get", new=AsyncMock(return_value=self.user)):
            url = await self.vpn.get_key(self.user)
            self.assertEqual(url, "https://sub.example/custom/shop-sub-id")
            self.adapter.get_client.return_value = replace(self.client, sub_id="../other")
            with self.assertRaises(VPNReadError):
                await self.vpn.get_key(self.user)
