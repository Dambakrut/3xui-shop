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

    def use_xhttp(self):
        from app.integrations.xui import XUIInboundSummary

        data = json.loads((ROOT / "tests/fixtures/xui/v3_8_5/inbound_xhttp.json").read_text())
        self.inbound = XUIInboundSummary.from_api(data)
        self.config.xui.INBOUND_ID = 6
        self.pool.validate_configured_inbound.return_value = self.inbound
        self.adapter.get_inbound.return_value = self.inbound
        self.client = replace(self.client, flow="", inbound_ids=(99, 6))
        self.adapter.get_client.return_value = self.client

    async def test_xhttp_create_resolves_empty_flow_and_only_configured_membership(self):
        self.use_xhttp()
        self.assertTrue(await self.vpn.create_client(self.user, 2, 30))
        write = self.adapter.add_client.await_args.args[0]
        self.assertEqual(write.flow, "")
        self.assertEqual(write.client_payload()["flow"], "")
        self.assertNotIn("xtls-rprx-vision", json.dumps(write.client_payload()))
        self.assertEqual(write.inbound_ids, (6,))
        self.assertEqual(write.uuid, UUID)

    async def test_xhttp_explicit_vision_and_unknown_flows_fail_without_post(self):
        self.use_xhttp()
        for flow in ("xtls-rprx-vision", "xtls-rprx-vision-udp443", "unknown"):
            with self.subTest(flow=flow):
                self.assertFalse(await self.vpn.create_client(self.user, 2, 30, flow=flow))
        self.adapter.add_client.assert_not_awaited()

    async def test_xhttp_disabled_or_unsupported_capabilities_fail_without_post(self):
        self.use_xhttp()
        for inbound in (
            replace(self.inbound, enable=False),
            replace(self.inbound, protocol="vmess"),
            replace(self.inbound, stream_settings={**self.inbound.stream_settings, "security": "none"}),
            replace(self.inbound, stream_settings={**self.inbound.stream_settings, "security": "tls"}),
            replace(self.inbound, stream_settings={**self.inbound.stream_settings, "network": "grpc"}),
            replace(self.inbound, stream_settings={"security": "reality", "network": "xhttp"}),
            replace(self.inbound, stream_settings={**self.inbound.stream_settings,
                    "xhttpSettings": {"mode": "unknown"}}),
            replace(self.inbound, raw={**self.inbound.raw, "disableFlow": "false"}),
        ):
            self.pool.validate_configured_inbound.return_value = inbound
            with self.subTest(inbound=inbound):
                self.assertFalse(await self.vpn.create_client(self.user, 2, 30))
        self.adapter.add_client.assert_not_awaited()

    async def test_xhttp_disable_flow_does_not_disable_empty_flow_clients(self):
        self.use_xhttp()
        self.pool.validate_configured_inbound.return_value = replace(
            self.inbound, raw={**self.inbound.raw, "disableFlow": True},
        )
        self.assertTrue(await self.vpn.create_client(self.user, 2, 30))
        self.assertEqual(self.adapter.add_client.await_args.args[0].flow, "")
        self.adapter.get_inbound.return_value = self.pool.validate_configured_inbound.return_value
        self.assertTrue(await self.vpn.update_client(self.user, 2, 30))
        self.assertEqual(self.adapter.update_client.await_args.args[1].flow, "")

    def test_xhttp_transport_modes_leave_client_flow_empty(self):
        self.use_xhttp()
        for mode in ("", "auto", "stream-one", "stream-up", "packet-up"):
            inbound = replace(self.inbound, stream_settings={
                **self.inbound.stream_settings, "xhttpSettings": {"mode": mode},
            })
            with self.subTest(mode=mode):
                self.assertEqual(self.vpn._validate_provisioning_capability(inbound, None), "")

    async def test_xhttp_vless_encryption_does_not_implicitly_enable_vision(self):
        self.use_xhttp()
        self.pool.validate_configured_inbound.return_value = replace(self.inbound, raw={
            **self.inbound.raw, "settings": {"flow": "", "encryption": "synthetic-vlessenc"},
        })
        self.assertTrue(await self.vpn.create_client(self.user, 2, 30))
        self.assertEqual(self.adapter.add_client.await_args.args[0].flow, "")
        self.adapter.add_client.reset_mock()
        self.assertFalse(await self.vpn.create_client(self.user, 2, 30, flow="xtls-rprx-vision"))
        self.adapter.add_client.assert_not_awaited()

    async def test_xhttp_unknown_activation_keeps_payment_review_and_duplicate_noop(self):
        self.use_xhttp()
        await self.test_unknown_activation_result_enters_payment_review_without_rewards_or_retry()

    async def test_empty_flow_cannot_inherit_global_vision_or_unknown_settings(self):
        self.use_xhttp()
        for settings in (None, "{}", {"flow": "xtls-rprx-vision"}, {"flow": None}):
            inbound = replace(self.inbound, raw={**self.inbound.raw, "settings": settings})
            self.pool.validate_configured_inbound.return_value = inbound
            self.adapter.get_inbound.return_value = inbound
            with self.subTest(settings=settings):
                self.assertFalse(await self.vpn.create_client(self.user, 2, 30))
                self.assertFalse(await self.vpn.update_client(self.user, 2, 30))
        self.adapter.add_client.assert_not_awaited()
        self.adapter.update_client.assert_not_awaited()

    async def test_xhttp_update_preserves_fields_and_checks_every_attachment(self):
        self.use_xhttp()
        self.assertTrue(await self.vpn.update_client(self.user, 2, 30, replace_devices=True))
        current, write = self.adapter.update_client.await_args.args
        self.assertIs(current, self.client)
        self.assertEqual(write.inbound_ids, (99, 6))
        self.assertEqual([call.args[0] for call in self.adapter.get_inbound.await_args_list], [99, 6])
        for field in ("flow", "total_bytes", "sub_id", "limit_hwid", "comment", "reset"):
            self.assertEqual(getattr(write, field), getattr(self.client, field))

    async def test_xhttp_shared_unsupported_attachment_blocks_update_in_any_order(self):
        self.use_xhttp()
        for other in (
            replace(self.inbound, protocol="trojan"),
            replace(self.inbound, enable=False),
            replace(self.inbound, stream_settings={"security": "reality", "network": "grpc"}),
            replace(self.inbound, stream_settings={**self.inbound.stream_settings, "security": "tls"}),
        ):
            self.adapter.get_inbound.side_effect = lambda inbound_id: self.inbound if inbound_id == 6 else other
            for memberships in ((99, 6), (6, 99)):
                self.adapter.get_client.return_value = replace(self.client, inbound_ids=memberships)
                with self.subTest(other=other, memberships=memberships):
                    self.assertFalse(await self.vpn.update_client(self.user, 2, 30))
        self.adapter.update_client.assert_not_awaited()

    async def test_xhttp_existing_vision_is_not_silently_normalized(self):
        self.use_xhttp()
        self.adapter.get_client.return_value = replace(self.client, flow="xtls-rprx-vision")
        self.assertFalse(await self.vpn.update_client(self.user, 2, 30))
        self.adapter.update_client.assert_not_awaited()

    async def test_shared_tcp_xhttp_empty_flow_preserved_and_flow_override_rejected(self):
        self.use_xhttp()
        tcp = replace(self.inbound, stream_settings={"security": "tls", "network": "tcp"})
        self.adapter.get_inbound.side_effect = lambda inbound_id: self.inbound if inbound_id == 6 else tcp
        self.assertTrue(await self.vpn.update_client(self.user, 2, 30))
        self.assertEqual(self.adapter.update_client.await_args.args[1].flow, "")
        self.adapter.update_client.reset_mock()
        self.assertFalse(await self.vpn.update_client(self.user, 2, 30, flow="xtls-rprx-vision"))
        self.adapter.update_client.assert_not_awaited()

    def test_tcp_tls_and_reality_defaults_preserve_vision(self):
        for security in ("tls", "reality"):
            inbound = replace(self.inbound, stream_settings={"security": security, "network": "tcp"})
            with self.subTest(security=security):
                self.assertEqual(self.vpn._validate_provisioning_capability(inbound, None), "xtls-rprx-vision")

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
