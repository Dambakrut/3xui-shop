"""Local mocks and loopback only; never loads production .env."""

import contextlib
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

AVAILABLE = all(importlib.util.find_spec(name) for name in ("aiohttp", "dotenv"))


@unittest.skipUnless(AVAILABLE, "Requires locked runtime dependencies")
class SmokeConfigTests(unittest.TestCase):
    def setUp(self):
        from scripts import xui_live_readonly_smoke as smoke
        self.smoke = smoke
        self.env = {"XUI_HOST": "https://panel.example/base", "XUI_AUTH_MODE": "token",
                    "XUI_API_TOKEN": "sensitive-api-token", "XUI_INBOUND_ID": "6"}

    def test_config_token_only_and_invalid_config_fails_closed(self):
        config = self.smoke.SmokeConfig.from_env(self.env)
        self.assertEqual(config.inbound_id, 6)
        self.assertNotIn(self.env["XUI_API_TOKEN"], repr(config))
        for changes in ({"XUI_AUTH_MODE": "session"}, {"XUI_API_TOKEN": ""},
                        {"XUI_HOST": "http://panel.example/base"}, {"XUI_INBOUND_ID": "0"},
                        {"XUI_HOST": "https://panel.example/base?secret=value"},
                        {"XUI_SMOKE_CLIENT_EMAIL": "../other"}):
            with self.subTest(changes=changes), self.assertRaises(self.smoke.SmokeError):
                self.smoke.SmokeConfig.from_env({**self.env, **changes})

    def test_redaction_hides_full_uuid_subid_token_and_urls(self):
        uuid = "11111111-1111-4111-8111-111111111111"
        sub_id = "abcdefghijklmnop"
        self.assertNotIn(uuid, self.smoke.redact(uuid))
        self.assertNotIn(sub_id, self.smoke.redact(sub_id))
        self.assertEqual(self.smoke.redact("short"), "<redacted>")
        summary = self.smoke.safe_summary(
            {"remark": "sensitive-api-token", "link": "vless://sensitive-full-link"},
            ("sensitive-api-token",),
        )
        self.assertNotIn("sensitive-api-token", json.dumps(summary))
        self.assertNotIn("vless://", json.dumps(summary))

    def test_dry_run_cli_has_no_http_or_secret_output(self):
        with patch.object(self.smoke, "load_local_env", return_value=self.env), \
                patch.object(self.smoke, "ReadOnlySmokeAdapter") as adapter, \
                patch.object(self.smoke.aiohttp, "ClientSession") as session, \
                patch.object(self.smoke.logging, "disable"), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(self.smoke.main(["--dry-run"]), 0)
        adapter.assert_not_called()
        session.assert_not_called()
        self.assertNotIn(self.env["XUI_API_TOKEN"], output.getvalue())
        result = json.loads(output.getvalue())
        self.assertEqual(result["network_requests"], 0)
        self.assertEqual(result["planned_requests"], [
            {"method": "GET", "path": "/base/panel/api/server/status"},
            {"method": "GET", "path": "/base/panel/api/inbounds/list"},
        ])


@unittest.skipUnless(AVAILABLE, "Requires locked runtime dependencies")
class SmokeGuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from scripts import xui_live_readonly_smoke as smoke
        self.smoke = smoke
        self.config = smoke.SmokeConfig("https://panel.example/base", "secret-token", 6,
                                        client_email="own-client", sub_id="own-subscription")

    async def test_allowed_get_and_settings_post_reach_adapter(self):
        adapter = self.smoke.ReadOnlySmokeAdapter(self.config)
        with patch.object(self.smoke.XUIAdapter, "_request", new=AsyncMock(return_value={})) as http:
            for method, endpoint in self.config.request_plan():
                await adapter._request(method, endpoint, json_body={} if method == "POST" else None)
            self.assertEqual(http.await_count, 5)
        await adapter.close()

    async def test_client_mutations_unknown_posts_and_session_auth_blocked(self):
        adapter = self.smoke.ReadOnlySmokeAdapter(self.config)
        with patch.object(self.smoke.XUIAdapter, "_request", new=AsyncMock()) as http:
            for method, endpoint in (
                ("POST", "panel/api/clients/add"),
                ("POST", "panel/api/clients/update/own-client"),
                ("POST", "panel/api/inbounds/addClient"),
                ("POST", "panel/api/inbounds/updateClient/uuid"),
                ("POST", "panel/api/setting/update"),
                ("POST", "unknown"), ("POST", "login"), ("GET", "csrf-token"),
                ("GET", "panel/api/clients/get/someone-else"),
            ):
                with self.subTest(endpoint=endpoint), self.assertRaises(self.smoke.SmokeError):
                    await adapter._request(method, endpoint)
            with self.assertRaises(self.smoke.SmokeError):
                await adapter._request("POST", "panel/api/setting/all", json_body={"update": True})
            with self.assertRaises(self.smoke.SmokeError):
                await adapter.add_client(object())
            with self.assertRaises(self.smoke.SmokeError):
                await adapter.update_client(object(), object())
            http.assert_not_awaited()
            self.assertEqual(adapter.requests_attempted, 0)
        await adapter.close()

    def fake(self):
        from app.integrations.xui import XUIClient, XUIInboundSummary, XUIServerStatus
        from app.integrations.xui import XUIClientTraffic
        root = Path(__file__).resolve().parent / "fixtures/xui/v3_8_5"
        def data(name):
            return json.loads((root / (name + ".json")).read_text())['obj']
        wrapper = data("client")
        wrapper["client"]["email"] = self.config.client_email
        wrapper["inboundIds"] = [99, 6]
        inbound = data("inbounds")[1]
        inbound.update(id=6, disableFlow=False)
        return SimpleNamespace(
            get_server_status=AsyncMock(return_value=XUIServerStatus.from_api(data("server_status"))),
            get_inbound=AsyncMock(return_value=XUIInboundSummary.from_api(inbound)),
            get_client=AsyncMock(return_value=XUIClient.from_api(wrapper)),
            get_client_traffic=AsyncMock(return_value=XUIClientTraffic.from_api(data("traffic"))),
            get_subscription_base_url=AsyncMock(return_value="https://sub.example/custom/"),
            requests_attempted=0,
        )

    async def run_fake(self, fake):
        context = AsyncMock()
        context.__aenter__.return_value = fake
        with patch.object(self.smoke, "ReadOnlySmokeAdapter", return_value=context):
            return await self.smoke.run_smoke(self.config, dry_run=False)

    async def test_success_summary_has_memberships_and_redacted_identity(self):
        fake = self.fake()
        result = await self.run_fake(fake)
        self.assertEqual(result["outcome"], "PASS")
        self.assertTrue(result["client"]["shared_client"])
        self.assertEqual(result["client"]["inbound_ids"], [99, 6])
        self.assertFalse(result["traffic"]["numeric_id_used_as_uuid"])
        self.assertNotIn(fake.get_client.return_value.uuid, json.dumps(result))
        self.assertNotIn(fake.get_client.return_value.sub_id, json.dumps(result))
        self.assertNotIn(self.config.token, json.dumps(result))

    async def test_version_mismatch_stops_before_inbound(self):
        from dataclasses import replace
        fake = self.fake()
        fake.get_server_status.return_value = replace(fake.get_server_status.return_value, panel_version="3.8.4")
        result = await self.run_fake(fake)
        self.assertEqual(result["outcome"], "FAIL")
        self.assertEqual(result["failure"]["stage"], "server_status")
        fake.get_inbound.assert_not_awaited()

    async def test_missing_inbound_stops_before_client(self):
        from app.integrations.xui import XUINotFoundError
        fake = self.fake()
        fake.get_inbound.side_effect = XUINotFoundError("safe")
        result = await self.run_fake(fake)
        self.assertEqual(result["failure"]["stage"], "inbound")
        fake.get_client.assert_not_awaited()

    async def test_membership_mismatch_stops_before_traffic(self):
        from dataclasses import replace
        fake = self.fake()
        fake.get_client.return_value = replace(fake.get_client.return_value, inbound_ids=(99,))
        result = await self.run_fake(fake)
        self.assertEqual(result["outcome"], "FAIL")
        fake.get_client_traffic.assert_not_awaited()
        fake.get_subscription_base_url.assert_not_awaited()

    async def test_auth_permission_failure_has_safe_diagnostic_and_no_fallback(self):
        from app.integrations.xui import XUIAuthorizationError
        fake = self.fake()
        fake.get_server_status.side_effect = XUIAuthorizationError("Authorization: secret-token")
        result = await self.run_fake(fake)
        self.assertEqual(result["failure"]["error_type"], "XUIAuthorizationError")
        self.assertNotIn("secret-token", json.dumps(result))
        fake.get_inbound.assert_not_awaited()

    async def test_guard_wire_bearer_no_cookies_and_only_allowed_requests(self):
        from aiohttp import web
        requests = []
        async def handler(request):
            requests.append((request.method, request.path, request.headers.get("Cookie")))
            self.assertEqual(request.headers.get("Authorization"), "Bearer secret-token")
            response = web.json_response({"success": True, "obj": {}})
            response.set_cookie("3x-ui", "should-not-be-sent")
            return response
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", handler)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        # Direct test config only: CLI from_env requires HTTPS for production.
        config = self.smoke.SmokeConfig(f"http://127.0.0.1:{port}/base", "secret-token", 6,
                                        sub_id="dummy")
        try:
            async with self.smoke.ReadOnlySmokeAdapter(config) as adapter:
                await adapter._request("GET", "panel/api/server/status")
                await adapter._request("POST", "panel/api/setting/all", json_body={})
                with self.assertRaises(self.smoke.SmokeError):
                    await adapter._request("POST", "panel/api/clients/add", json_body={})
                self.assertEqual(adapter.requests_attempted, 2)
            self.assertEqual(requests, [("GET", "/base/panel/api/server/status", None),
                                        ("POST", "/base/panel/api/setting/all", None)])
        finally:
            await runner.cleanup()

    async def test_dropped_get_is_not_retried_by_aiohttp(self):
        from aiohttp import web
        from app.integrations.xui import XUITransportError
        calls = []
        async def handler(request):
            calls.append(request.path)
            request.transport.close()
            return web.Response()
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", handler)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        config = self.smoke.SmokeConfig(f"http://127.0.0.1:{port}/base", "dummy-token", 6)
        original = self.smoke.aiohttp.client.IDEMPOTENT_METHODS
        try:
            async with self.smoke.ReadOnlySmokeAdapter(config) as adapter:
                with self.assertRaises(XUITransportError):
                    await adapter.get_server_status()
                self.assertEqual(adapter.requests_attempted, 1)
            self.assertEqual(calls, ["/base/panel/api/server/status"])
            self.assertIs(self.smoke.aiohttp.client.IDEMPOTENT_METHODS, original)
        finally:
            await runner.cleanup()
