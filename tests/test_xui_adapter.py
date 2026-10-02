"""Loopback HTTP contract tests for the read-only v3.8.5 adapter."""

import asyncio
import ast
import importlib.util
import json
import logging
from pathlib import Path
import socket
import unittest

AVAILABLE = importlib.util.find_spec("aiohttp") is not None
ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "xui" / "v3_8_5"
UUID = "11111111-1111-4111-8111-111111111111"


def fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


@unittest.skipUnless(AVAILABLE, "Requires locked aiohttp runtime")
class XUIAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from aiohttp import web

        self.calls = []
        self.overrides = {}
        self.login_payload = None
        self.server_secret = "local-password-secret"
        self.token_secret = "local-bearer-secret"

        async def handler(request):
            self.calls.append((request.method, request.path, dict(request.headers),
                               request.cookies.get("3x-ui")))
            if request.path in self.overrides:
                override = self.overrides[request.path]
                if callable(override):
                    return await override(request)
                return override
            if request.path == "/base/csrf-token":
                response = web.json_response({"success": True, "obj": "local-csrf-secret"})
                response.set_cookie("3x-ui", "bootstrap-cookie", path="/base")
                return response
            if request.path == "/base/login":
                body = await request.json()
                self.login_payload = body
                if (request.cookies.get("3x-ui") != "bootstrap-cookie"
                        or request.headers.get("X-CSRF-Token") != "local-csrf-secret"
                        or body.get("username") != "local-user"
                        or body.get("password") != self.server_secret):
                    return web.Response(status=403)
                response = web.json_response({"success": True, "msg": "ok"})
                response.set_cookie("3x-ui", "logged-in-cookie", path="/base")
                return response
            if (request.cookies.get("3x-ui") != "logged-in-cookie"
                    and request.headers.get("Authorization") != f"Bearer {self.token_secret}"):
                return web.Response(status=401)
            data = {
                "/base/panel/api/server/status": "server_status",
                "/base/panel/api/inbounds/list": "inbounds",
                "/base/panel/api/clients/get/123": "client",
                "/base/panel/api/clients/traffic/123": "traffic",
                "/base/panel/api/clients/subLinks/shop-sub-id": "sub_links",
            }.get(request.path)
            if data:
                return web.json_response(fixture(data))
            return web.Response(status=404)

        self.app = web.Application()
        self.app.router.add_route("*", "/{tail:.*}", handler)
        self.runner = web.AppRunner(self.app, access_log=None)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        port = self.site._server.sockets[0].getsockname()[1]
        self.base_url = f"http://localhost:{port}/base"
        self.adapters = []

    async def asyncTearDown(self):
        for adapter in self.adapters:
            await adapter.close()
        await self.runner.cleanup()

    def adapter(self, *, mode="session", timeout=1.0):
        from app.integrations.xui import XUIAdapter, XUIAuthMode

        if mode == "session":
            kwargs = {"auth_mode": XUIAuthMode.SESSION, "username": "local-user",
                      "password": self.server_secret, "two_factor_code": "123456"}
        else:
            kwargs = {"auth_mode": XUIAuthMode.TOKEN, "token": self.token_secret}
        result = XUIAdapter(self.base_url, timeout_seconds=timeout, **kwargs)
        self.adapters.append(result)
        return result

    async def test_session_auth_csrf_login_and_cookie_continuity(self):
        adapter = self.adapter()
        await adapter.authenticate()
        status = await adapter.get_server_status()
        self.assertEqual(status.panel_version, "v3.8.5")
        self.assertEqual(status.xray_version, "26.9.30")
        self.assertEqual([call[1] for call in self.calls],
                         ["/base/csrf-token", "/base/login", "/base/panel/api/server/status"])
        self.assertEqual(self.calls[1][3], "bootstrap-cookie")
        self.assertEqual(self.calls[1][2]["X-CSRF-Token"], "local-csrf-secret")
        self.assertEqual(self.login_payload["twoFactorCode"], "123456")
        self.assertEqual(self.calls[2][3], "logged-in-cookie")
        self.assertEqual(adapter.csrf_token, "local-csrf-secret")
        self.assertNotIn("Authorization", self.calls[2][2])

    async def test_bad_login_and_bad_csrf_are_auth_errors(self):
        from aiohttp import web
        from app.integrations.xui import XUIAuthenticationError, XUIProtocolError

        self.overrides["/base/login"] = web.json_response({"success": False, "msg": "secret"})
        adapter = self.adapter()
        with self.assertRaises(XUIAuthenticationError):
            await adapter.authenticate()
        self.assertIsNone(adapter.csrf_token)
        self.overrides.pop("/base/login")
        self.overrides["/base/csrf-token"] = web.json_response({"success": True, "obj": ""})
        with self.assertRaises(XUIProtocolError):
            await adapter.authenticate()
        self.assertNotIn("/base/login", [call[1] for call in self.calls[3:]])

    async def test_csrf_mismatch_rejected_by_login(self):
        from aiohttp import web
        from app.integrations.xui import XUIAuthenticationError

        async def wrong_csrf(_request):
            response = web.json_response({"success": True, "obj": "wrong-csrf"})
            response.set_cookie("3x-ui", "bootstrap-cookie", path="/base")
            return response

        self.overrides["/base/csrf-token"] = wrong_csrf
        adapter = self.adapter()
        with self.assertRaises(XUIAuthenticationError):
            await adapter.authenticate()
        self.assertIsNone(adapter.csrf_token)
        self.assertEqual([call[1] for call in self.calls], ["/base/csrf-token", "/base/login"])

    async def test_token_sends_bearer_and_skips_login(self):
        adapter = self.adapter(mode="token")
        await adapter.authenticate()
        status = await adapter.get_server_status()
        self.assertEqual(status.panel_version, "v3.8.5")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][2]["Authorization"], f"Bearer {self.token_secret}")
        self.assertIsNone(adapter.csrf_token)

    async def test_inbound_list_and_exact_id_lookup(self):
        from app.integrations.xui import XUINotFoundError

        adapter = self.adapter()
        inbounds = await adapter.list_inbounds()
        self.assertEqual([inbound.id for inbound in inbounds], [99, 42])
        self.assertEqual((await adapter.get_inbound(42)).protocol, "vless")
        self.assertEqual((await adapter.get_inbound(42)).stream_settings["security"], "reality")
        with self.assertRaises(XUINotFoundError):
            await adapter.get_inbound(1)

    async def test_canonical_client_uuid_all_memberships_and_preserved_fields(self):
        from app.integrations.xui import is_member_of, validate_client_membership

        client = await self.adapter().get_client("123")
        self.assertEqual(client.record_id, 73)
        self.assertEqual(client.uuid, UUID)
        self.assertEqual(client.inbound_ids, (99, 42))
        self.assertEqual(client.total_bytes, 1073741824)
        self.assertEqual(client.expiry_time_ms, 1800000000000)
        self.assertEqual(client.raw["security"], "auto")
        self.assertTrue(is_member_of(client, 42))
        self.assertTrue(is_member_of(client, 99))
        validate_client_membership(client, 42)

    async def test_single_membership_and_rejection_of_other_inbound(self):
        from aiohttp import web
        from app.integrations.xui import XUIAuthorizationError, validate_client_membership

        payload = fixture("client")
        payload["obj"]["inboundIds"] = [42]
        self.overrides["/base/panel/api/clients/get/123"] = web.json_response(payload)
        client = await self.adapter().get_client("123")
        self.assertEqual(client.inbound_ids, (42,))
        with self.assertRaises(XUIAuthorizationError):
            validate_client_membership(client, 99)

    async def test_traffic_numeric_id_is_distinct_from_uuid(self):
        traffic = await self.adapter().get_client_traffic("123")
        self.assertEqual(traffic.id, 14825)
        self.assertIs(type(traffic.id), int)
        self.assertEqual(traffic.uuid, UUID)
        self.assertEqual(traffic.total, 1073741824)

    async def test_traffic_null_is_explicitly_absent(self):
        from aiohttp import web

        self.overrides["/base/panel/api/clients/traffic/123"] = web.json_response(
            {"success": True, "obj": None})
        self.assertIsNone(await self.adapter().get_client_traffic("123"))

    async def test_protocol_share_links_are_returned_unchanged(self):
        links = await self.adapter().get_subscription_links("shop-sub-id")
        self.assertEqual(len(links), 1)
        self.assertTrue(links[0].startswith("vless://"))

    async def test_success_false_and_not_found_are_typed(self):
        from aiohttp import web
        from app.integrations.xui import XUIAPIError, XUINotFoundError

        path = "/base/panel/api/clients/get/123"
        self.overrides[path] = web.json_response({"success": False, "msg": "(record not found)"})
        adapter = self.adapter()
        with self.assertRaises(XUINotFoundError):
            await adapter.get_client("123")
        self.overrides[path] = web.json_response({"success": False, "msg": "database unavailable"})
        with self.assertRaises(XUIAPIError):
            await adapter.get_client("123")

    async def test_malformed_json_envelope_and_required_obj(self):
        from aiohttp import web
        from app.integrations.xui import XUIProtocolError

        path = "/base/panel/api/server/status"
        adapter = self.adapter()
        self.overrides[path] = web.Response(text="not json", content_type="application/json")
        with self.assertRaises(XUIProtocolError):
            await adapter.get_server_status()
        self.overrides[path] = web.json_response({"success": True})
        with self.assertRaises(XUIProtocolError):
            await adapter.get_server_status()
        self.overrides[path] = web.json_response({"success": "true", "obj": {}})
        with self.assertRaises(XUIProtocolError):
            await adapter.get_server_status()

    async def test_http_error_and_forbidden_are_typed(self):
        from aiohttp import web
        from app.integrations.xui import XUITransportError, XUIAuthorizationError

        path = "/base/panel/api/server/status"
        adapter = self.adapter(mode="token")
        self.overrides[path] = web.Response(status=503, text="secret response")
        with self.assertRaises(XUITransportError):
            await adapter.get_server_status()
        self.overrides[path] = web.Response(status=403)
        with self.assertRaises(XUIAuthorizationError):
            await adapter.get_server_status()

    async def test_timeout_and_connection_failure_are_typed(self):
        from aiohttp import web
        from app.integrations.xui import XUIAdapter, XUIAuthMode, XUITransportError

        async def delayed(_request):
            await asyncio.sleep(0.2)
            return web.json_response(fixture("server_status"))

        self.overrides["/base/panel/api/server/status"] = delayed
        with self.assertRaises(XUITransportError):
            await self.adapter(mode="token", timeout=0.02).get_server_status()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        adapter = XUIAdapter(f"http://127.0.0.1:{port}", auth_mode=XUIAuthMode.TOKEN,
                             token="dummy", timeout_seconds=0.1)
        self.adapters.append(adapter)
        with self.assertRaises(XUITransportError):
            await adapter.get_server_status()

    async def test_context_manager_closes_owned_session(self):
        from app.integrations.xui import XUIError

        adapter = self.adapter(mode="token")
        async with adapter:
            await adapter.get_server_status()
            session = adapter._session
            self.assertFalse(session.closed)
        self.assertTrue(session.closed)
        with self.assertRaises(XUIError):
            await adapter.get_server_status()

    async def test_separate_adapter_instances_do_not_share_cookies(self):
        first = self.adapter()
        second = self.adapter()
        await first.authenticate()
        await second.authenticate()
        self.assertIsNot(first._session, second._session)
        self.assertIsNot(first._session.cookie_jar, second._session.cookie_jar)
        self.assertEqual([call[1] for call in self.calls].count("/base/login"), 2)

    async def test_secrets_and_payloads_are_not_logged_or_in_errors(self):
        from aiohttp import web
        from app.integrations.xui import XUIAuthenticationError

        records = []

        class Collector(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        logger = logging.getLogger("app.integrations.xui")
        collector = Collector()
        logger.addHandler(collector)
        try:
            self.overrides["/base/login"] = web.json_response(
                {"success": False, "msg": self.server_secret + self.token_secret})
            with self.assertRaises(XUIAuthenticationError) as raised:
                await self.adapter().authenticate()
        finally:
            logger.removeHandler(collector)
        output = "\n".join(records) + str(raised.exception)
        for secret in (self.server_secret, self.token_secret, "local-csrf-secret", "bootstrap-cookie"):
            self.assertNotIn(secret, output)

    async def test_no_legacy_paths_or_py3xui_imports(self):
        adapter = self.adapter(mode="token")
        await adapter.get_server_status()
        await adapter.list_inbounds()
        await adapter.get_client("123")
        await adapter.get_client_traffic("123")
        await adapter.get_subscription_links("shop-sub-id")
        paths = [call[1] for call in self.calls]
        for old in ("getClientTraffics", "addClient", "updateClient"):
            self.assertFalse(any(old in path for path in paths))
        self.assertTrue(all(call[0] == "GET" for call in self.calls))
        for source in (ROOT / "app" / "integrations" / "xui").glob("*.py"):
            code = source.read_text(encoding="utf-8")
            tree = ast.parse(code)
            imports = [node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))]
            for node in imports:
                names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                self.assertFalse(any(name.startswith("py3xui") for name in names), source.name)
            if source.name == "adapter.py":
                for old in ("getClientTraffics", "addClient", "updateClient"):
                    self.assertNotIn(old, code)


if __name__ == "__main__":
    unittest.main()
