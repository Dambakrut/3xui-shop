"""Loopback-only wire tests for v3.8.5 client mutations."""

import asyncio
import copy
import importlib.util
import json
import unittest

AVAILABLE = importlib.util.find_spec("aiohttp") is not None
UUID = "11111111-1111-4111-8111-111111111111"


@unittest.skipUnless(AVAILABLE, "Requires locked aiohttp runtime")
class XUIWriteWireTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from aiohttp import web

        self.record = None
        self.memberships = [42]
        self.posts = []
        self.post_behavior = "normal"
        self.pending = False

        async def handler(request):
            path = request.path
            if path == "/base/csrf-token":
                response = web.json_response({"success": True, "obj": "csrf-secret"})
                response.set_cookie("3x-ui", "bootstrap", path="/base")
                return response
            if path == "/base/login":
                if request.headers.get("X-CSRF-Token") != "csrf-secret":
                    return web.Response(status=403)
                response = web.json_response({"success": True})
                response.set_cookie("3x-ui", "session", path="/base")
                return response
            if (request.cookies.get("3x-ui") != "session"
                    and request.headers.get("Authorization") != "Bearer admin-token"):
                return web.Response(status=401)
            if path == "/base/panel/api/clients/get/123":
                if self.record is None:
                    return web.json_response({"success": False, "msg": "record not found", "obj": None})
                return web.json_response({"success": True, "obj": {
                    "client": self.record, "inboundIds": self.memberships,
                }})
            if path == "/base/panel/api/inbounds/list":
                return web.json_response({"success": True, "obj": self.inbounds})
            if path == "/base/panel/api/setting/all":
                self.settings_headers = dict(request.headers)
                return web.json_response({"success": True, "obj": {
                    "subURI": getattr(self, "sub_uri", "https://sub.example/custom/path/"),
                }})
            if path in ("/base/panel/api/clients/add", "/base/panel/api/clients/update/123"):
                self.posts.append((path, dict(request.headers), await request.json(), request.query_string))
                body = self.posts[-1][2]
                if self.post_behavior not in ("old", "reject_old", "mismatch"):
                    self.record = dict(body["client"] if "client" in body else body)
                    self.record["id"] = 73
                    self.record["uuid"] = body.get("client", body)["id"]
                    self.record["allowedIPs"] = json.dumps(self.record["allowedIPs"])
                    if "inboundIds" in body:
                        self.memberships = body["inboundIds"]
                if self.post_behavior in ("timeout", "old"):
                    await asyncio.sleep(1.5)
                if self.post_behavior == "malformed":
                    return web.Response(text="not JSON")
                if self.post_behavior == "drop":
                    request.transport.close()
                    return web.Response()
                if self.post_behavior in ("rejected", "reject_old"):
                    return web.json_response({"success": False, "msg": "partial failure", "obj": None})
                if self.post_behavior == "null_obj":
                    return web.json_response({"success": True, "obj": None})
                return web.json_response({"success": True, "obj": {"nodePending": self.pending}})
            return web.Response(status=404)

        self.app = web.Application()
        self.app.router.add_route("*", "/{tail:.*}", handler)
        self.runner = web.AppRunner(self.app, access_log=None)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        port = self.site._server.sockets[0].getsockname()[1]
        self.url = f"http://localhost:{port}/base"
        self.adapters = []

    async def asyncTearDown(self):
        for adapter in self.adapters:
            await adapter.close()
        await self.runner.cleanup()

    def adapter(self, mode="session", timeout=1):
        from app.integrations.xui import XUIAdapter, XUIAuthMode

        options = ({"auth_mode": XUIAuthMode.SESSION, "username": "u", "password": "p"}
                   if mode == "session" else
                   {"auth_mode": XUIAuthMode.TOKEN, "token": "admin-token"})
        adapter = XUIAdapter(self.url, timeout_seconds=timeout, **options)
        self.adapters.append(adapter)
        return adapter

    @staticmethod
    def desired(expiry=1900000000000, memberships=(42,)):
        from app.integrations.xui import XUIClientWrite

        return XUIClientWrite(
            email="123", uuid=UUID, inbound_ids=memberships,
            expiry_time_ms=expiry, total_bytes=0, limit_ip=2,
            limit_hwid=0, tg_id=123, sub_id=UUID,
            flow="xtls-rprx-vision", comment="keep", reset=0,
        )

    def stored(self, desired=None):
        desired = desired or self.desired()
        self.record = desired.client_payload()
        self.record["uuid"] = self.record.pop("id")
        self.record["id"] = 73
        self.record["allowedIPs"] = json.dumps(self.record["allowedIPs"])
        self.memberships = list(desired.inbound_ids)

    async def test_add_and_session_csrf_wire(self):
        result = await self.adapter().add_client(self.desired())
        self.assertEqual(result.client.uuid, UUID)
        self.assertIs(result.node_pending, False)
        self.assertEqual(len(self.posts), 1)
        path, headers, body, query = self.posts[0]
        self.assertEqual(path, "/base/panel/api/clients/add")
        self.assertEqual(headers["X-CSRF-Token"], "csrf-secret")
        self.assertEqual(body["inboundIds"], [42])
        self.assertEqual(body["client"]["expiryTime"], 1900000000000)
        self.assertEqual(body["client"]["totalGB"], 0)

    async def test_xhttp_vpn_create_and_shared_renewal_wire_without_vision(self):
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from app.bot.services.vpn import VPNService
        from app.integrations.xui import XUIInboundSummary

        data = json.loads((Path(__file__).parent / "fixtures/xui/v3_8_5/inbound_xhttp.json").read_text())
        self.inbounds = [dict(data, id=99), data]
        adapter = self.adapter(mode="token")
        inbound = XUIInboundSummary.from_api(data)
        user = SimpleNamespace(tg_id=123, vpn_id=UUID, server_id=7)
        connection = SimpleNamespace(adapter=adapter)
        pool = SimpleNamespace(
            get_connection=AsyncMock(return_value=connection),
            validate_configured_inbound=AsyncMock(return_value=inbound),
        )
        config = SimpleNamespace(xui=SimpleNamespace(INBOUND_ID=6))
        vpn = VPNService(config, None, pool)
        self.assertTrue(await vpn.create_client(user, 2, 30))
        path, headers, body, query = self.posts[0]
        self.assertEqual(path, "/base/panel/api/clients/add")
        self.assertEqual(body["inboundIds"], [6])
        self.assertEqual(body["client"]["flow"], "")
        self.assertNotIn("xtls-rprx-vision", json.dumps(body))
        self.assertEqual(headers["Authorization"], "Bearer admin-token")
        self.assertNotIn("X-CSRF-Token", headers)
        self.memberships = [99, 6]
        self.record.update({"totalGB": 1234567, "limitHwid": 3,
                            "comment": "preserve", "reset": 7})
        before = copy.deepcopy(self.record)
        self.assertTrue(await vpn.update_client(user, 4, 30, replace_devices=True))
        path, _headers, body, query = self.posts[1]
        self.assertEqual(path, "/base/panel/api/clients/update/123")
        self.assertEqual(query, "")
        for field in ("flow", "totalGB", "subId", "limitHwid", "comment", "reset"):
            self.assertEqual(body[field], before[field])
        self.assertEqual(body["expiryTime"], before["expiryTime"] + 30 * 86400000)
        self.assertEqual(self.memberships, [99, 6])
        self.assertNotIn("xtls-rprx-vision", json.dumps(body))
        self.assertEqual(len(self.posts), 2)

    async def test_existing_identical_skips_post(self):
        self.stored()
        result = await self.adapter().add_client(self.desired())
        self.assertTrue(result.reconciled)
        self.assertIsNone(result.node_pending)
        self.assertEqual(self.posts, [])

    async def test_update_already_desired_skips_post_with_unknown_activation(self):
        from app.integrations.xui import XUIClientWrite

        self.stored()
        adapter = self.adapter()
        current = await adapter.get_client("123")
        desired = XUIClientWrite.from_client(
            current, expiry_time_ms=2000000000000, limit_ip=current.limit_ip,
            enable=current.enable,
        )
        self.stored(desired)
        result = await adapter.update_client(current, desired)
        self.assertTrue(result.reconciled)
        self.assertIsNone(result.node_pending)
        self.assertEqual(result.client.expiry_time_ms, desired.expiry_time_ms)
        self.assertEqual(self.posts, [])

    async def test_success_null_obj_confirms_persistence_not_activation(self):
        from app.integrations.xui import XUIClientWrite

        self.post_behavior = "null_obj"
        for operation in ("add", "update"):
            self.posts.clear()
            adapter = self.adapter()
            desired = self.desired()
            with self.subTest(operation=operation):
                if operation == "add":
                    self.record = None
                    result = await adapter.add_client(desired)
                else:
                    self.stored()
                    current = await adapter.get_client("123")
                    desired = XUIClientWrite.from_client(
                        current, expiry_time_ms=2000000000000,
                        limit_ip=current.limit_ip, enable=current.enable,
                    )
                    result = await adapter.update_client(current, desired)
                self.assertTrue(result.reconciled)
                self.assertIsNone(result.node_pending)
                self.assertEqual(result.client.uuid, desired.uuid)
                self.assertEqual(result.client.expiry_time_ms, desired.expiry_time_ms)
                self.assertEqual(len(self.posts), 1)

    async def test_existing_uuid_or_membership_mismatch_refused(self):
        from app.integrations.xui import XUIAmbiguousWriteError

        for change in ("uuid", "memberships"):
            self.stored()
            if change == "uuid":
                self.record["uuid"] = "other-uuid"
            else:
                self.memberships = [99]
            with self.subTest(change=change), self.assertRaises(XUIAmbiguousWriteError):
                await self.adapter().add_client(self.desired())
        self.assertEqual(self.posts, [])

    async def test_update_preserves_fields_and_memberships(self):
        from app.integrations.xui import XUIClientWrite

        self.stored(self.desired(memberships=(99, 42)))
        self.record.update({"totalGB": 1234567, "limitHwid": 3, "flow": "xtls-rprx-vision",
                            "comment": "preserve", "reset": 7, "group": "staff", "password": "secret",
                            "allowedIPs": '["10.0.0.1/32"]', "reverse": {"tag": "reverse-keep"},
                            "resetDay": 15, "resetMax": 8, "trafficReset": "monthly",
                            "trafficResetDay": 15, "adTag": "preserve-ad"})
        adapter = self.adapter()
        current = await adapter.get_client("123")
        write = XUIClientWrite.from_client(current, expiry_time_ms=2000000000000,
                                           limit_ip=4, enable=True)
        result = await adapter.update_client(current, write)
        self.assertEqual(set(result.client.inbound_ids), {42, 99})
        path, _headers, body, query = self.posts[0]
        self.assertEqual(path, "/base/panel/api/clients/update/123")
        self.assertEqual(query, "")
        for key in ("totalGB", "limitHwid", "flow", "comment", "reset", "group", "password"):
            self.assertEqual(body[key], current.raw[key])
        self.assertEqual(body["expiryTime"], 2000000000000)
        self.assertEqual(body["limitIp"], 4)
        self.assertEqual(body["allowedIPs"], ["10.0.0.1/32"])
        for key in ("subId", "reverse", "resetDay", "resetMax", "trafficReset", "trafficResetDay", "adTag"):
            self.assertEqual(body[key], current.raw[key])
        self.assertNotIn("uuid", body)
        self.assertNotIn("createdAt", body)

    async def test_changed_snapshot_blocks_update_without_post(self):
        from app.integrations.xui import XUIClientWrite, XUIAmbiguousWriteError

        self.stored()
        adapter = self.adapter()
        current = await adapter.get_client("123")
        write = XUIClientWrite.from_client(current, expiry_time_ms=2000000000000,
                                           limit_ip=2, enable=True)
        self.record["comment"] = "external edit"
        with self.assertRaises(XUIAmbiguousWriteError):
            await adapter.update_client(current, write)
        self.assertEqual(self.posts, [])

    async def test_enable_disable_preserves_expiry_and_memberships(self):
        from app.integrations.xui import XUIClientWrite

        self.stored(self.desired(memberships=(99, 42)))
        adapter = self.adapter()
        for enable in (False, True):
            current = await adapter.get_client("123")
            desired = XUIClientWrite.from_client(
                current, expiry_time_ms=current.expiry_time_ms,
                limit_ip=current.limit_ip, enable=enable,
            )
            result = await adapter.update_client(current, desired)
            self.assertIs(result.client.enable, enable)
            self.assertEqual(result.client.expiry_time_ms, 1900000000000)
            self.assertEqual(result.client.inbound_ids, (99, 42))
        self.assertEqual(len(self.posts), 2)

    async def test_rejected_or_unconfirmed_write_is_review_not_retry(self):
        from app.integrations.xui import XUIReconciliationError

        for behavior in ("reject_old", "mismatch"):
            self.post_behavior = behavior
            with self.subTest(behavior=behavior), self.assertRaises(XUIReconciliationError):
                await self.adapter().add_client(self.desired())
        self.assertEqual(len(self.posts), 2)

    async def test_connection_drop_after_commit_reconciles_once(self):
        self.post_behavior = "drop"
        result = await self.adapter().add_client(self.desired())
        self.assertIsNone(result.node_pending)
        self.assertEqual(len(self.posts), 1)

    async def test_missing_or_malformed_preservation_fields_fail_closed(self):
        from app.integrations.xui import XUIClientWrite, XUIProtocolError

        for field, value in (("allowedIPs", "bad JSON"), ("resetDay", "15")):
            self.stored()
            self.record[field] = value
            current = await self.adapter().get_client("123")
            with self.subTest(field=field), self.assertRaises(XUIProtocolError):
                XUIClientWrite.from_client(current, expiry_time_ms=2000000000000,
                                           limit_ip=2, enable=True)
        self.stored()
        del self.record["resetMax"]
        current = await self.adapter().get_client("123")
        with self.assertRaises(XUIProtocolError):
            XUIClientWrite.from_client(current, expiry_time_ms=2000000000000,
                                       limit_ip=2, enable=True)
        self.assertEqual(self.posts, [])

    async def test_write_does_not_log_credentials_or_client_payload(self):
        import logging
        from unittest.mock import patch

        with patch.object(logging.Logger, "_log") as log:
            await self.adapter(mode="token").add_client(self.desired())
        output = str(log.call_args_list)
        for secret in ("admin-token", "csrf-secret", UUID, "https://sub.example"):
            self.assertNotIn(secret, output)

    async def test_token_write_bearer_without_csrf_or_login(self):
        await self.adapter(mode="token").add_client(self.desired())
        headers = self.posts[0][1]
        self.assertEqual(headers["Authorization"], "Bearer admin-token")
        self.assertNotIn("X-CSRF-Token", headers)

    async def test_node_pending_is_typed(self):
        self.pending = True
        result = await self.adapter().add_client(self.desired())
        self.assertTrue(result.node_pending)

    async def test_timeout_after_commit_reconciles_without_retry(self):
        self.post_behavior = "timeout"
        result = await self.adapter(timeout=1).add_client(self.desired())
        self.assertTrue(result.reconciled)
        self.assertIsNone(result.node_pending)
        self.assertEqual(len(self.posts), 1)

    async def test_timeout_without_commit_requires_review(self):
        from app.integrations.xui import XUIReconciliationError

        self.post_behavior = "old"
        with self.assertRaises(XUIReconciliationError):
            await self.adapter(timeout=1).add_client(self.desired())
        self.assertEqual(len(self.posts), 1)

    async def test_malformed_response_after_commit_requires_node_review(self):
        self.post_behavior = "malformed"
        result = await self.adapter().add_client(self.desired())
        self.assertIsNone(result.node_pending)
        self.assertEqual(len(self.posts), 1)

    async def test_success_false_can_be_partial_and_is_not_retried(self):
        self.post_behavior = "rejected"
        result = await self.adapter().add_client(self.desired())
        self.assertIsNone(result.node_pending)
        self.assertEqual(len(self.posts), 1)

    async def test_untrusted_raw_cannot_override_identity(self):
        from app.integrations.xui import XUIClientWrite

        with self.assertRaises(ValueError):
            XUIClientWrite(email="123", uuid=UUID, inbound_ids=(42,),
                           expiry_time_ms=1900000000000, total_bytes=0, limit_ip=2,
                           limit_hwid=0, tg_id=123, sub_id=UUID,
                           preserved={"email": "other"})

    async def test_panel_subscription_uri_requires_https_custom_path_and_csrf(self):
        from app.integrations.xui import XUIProtocolError

        adapter = self.adapter()
        self.assertEqual(await adapter.get_subscription_base_url(),
                         "https://sub.example/custom/path/")
        self.assertEqual(self.settings_headers["X-CSRF-Token"], "csrf-secret")
        for uri in ("", "http://sub.example/custom/", "https://sub.example/custom",
                    "https://sub.example/custom/?token=secret"):
            self.sub_uri = uri
            with self.subTest(uri=uri), self.assertRaises(XUIProtocolError):
                await adapter.get_subscription_base_url()
