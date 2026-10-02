"""Pinned py3xui 0.3.2 wire assumptions; synthetic replies, no panel access.

These tests document incompatibilities, rather than certify a live panel.
"""
import importlib.util
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

AVAILABLE = all(importlib.util.find_spec(name) for name in
                ("py3xui", "httpx", "sqlalchemy", "aiogram"))
UUID = "11111111-1111-4111-8111-111111111111"


@unittest.skipUnless(AVAILABLE, "Requires locked runtime environment")
class Py3xuiWireAuditTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import httpx
        from py3xui import AsyncApi
        self.httpx = httpx
        self.requests = []
        self.reply = {"success": True, "msg": "", "obj": None}
        self.status = 200
        original = httpx.AsyncClient

        def respond(request):
            self.requests.append(request)
            return httpx.Response(self.status, json=self.reply,
                                  headers={"set-cookie": "3x-ui=synthetic; Path=/"})

        transport = httpx.MockTransport(respond)
        self.patcher = patch("py3xui.async_api.async_api_base.httpx.AsyncClient",
                             side_effect=lambda **kw: original(transport=transport, **kw))
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.api = AsyncApi("https://panel.invalid/base", "dummy", "dummy", token="dummy-otp")
        self.api.client.session = "synthetic"
        self.api.inbound.session = "synthetic"

    async def test_login_uses_login_secret_json_without_csrf_or_bearer(self):
        await self.api.login()
        req = self.requests[0]
        self.assertEqual(req.url.path, "/base/login")
        self.assertEqual(req.method, "POST")
        self.assertEqual(json.loads(req.content)["loginSecret"], "dummy-otp")
        self.assertNotIn("x-csrf-token", req.headers)
        self.assertNotIn("authorization", req.headers)

    async def test_current_csrf_rejection_is_not_bootstrapped(self):
        self.status = 403
        with self.assertRaises(self.httpx.HTTPStatusError):
            await self.api.login()
        self.assertEqual(len(self.requests), 1)

    async def test_lookup_is_remote_traffic_endpoint_and_drops_uuid(self):
        self.reply["obj"] = {"id": 73, "uuid": UUID, "email": "123", "enable": True,
                             "inboundId": 42, "expiryTime": 1800000000000}
        client = await self.api.client.get_by_email("123")
        self.assertEqual(self.requests[0].url.path,
                         "/base/panel/api/inbounds/getClientTraffics/123")
        self.assertEqual(client.id, 73)
        self.assertFalse(hasattr(client, "uuid"))
        self.assertEqual(client.inbound_id, 42)

    async def test_missing_lookup_returns_none(self):
        self.assertIsNone(await self.api.client.get_by_email("123"))

    async def test_list_accepts_current_object_settings_and_preserves_ids(self):
        self.reply["obj"] = [
            {"id": inbound_id, "enable": True, "port": 443, "protocol": "vless",
             "settings": {"clients": [{"id": UUID, "email": "123", "enable": True}]},
             "streamSettings": {"network": "tcp", "security": "reality"},
             "sniffing": {"enabled": False}}
            for inbound_id in (99, 42)
        ]
        inbounds = await self.api.inbound.get_list()
        self.assertEqual([item.id for item in inbounds], [99, 42])
        self.assertEqual(inbounds[1].settings.clients[0].id, UUID)
        self.assertEqual(self.requests[0].url.path, "/base/panel/api/inbounds/list")

    async def test_new_client_wrapper_cannot_be_parsed_as_legacy_client(self):
        from pydantic import ValidationError
        self.reply["obj"] = {"client": {"id": 73, "uuid": UUID, "email": "123",
                                         "enable": True}, "inboundIds": [42, 99]}
        with self.assertRaises(ValidationError):
            await self.api.client.get_by_email("123")

    async def test_add_serializes_explicit_inbound_milliseconds_and_bytes(self):
        from py3xui import Client
        client = Client(id=UUID, email="123", enable=True, expiry_time=1800000000000,
                        total_gb=1073741824, limit_ip=2, sub_id=UUID)
        await self.api.client.add(42, [client])
        req = self.requests[0]
        self.assertEqual(req.url.path, "/base/panel/api/inbounds/addClient")
        body = json.loads(req.content)
        self.assertEqual(body["id"], 42)
        self.assertIsInstance(body["settings"], str)
        wire = json.loads(body["settings"])["clients"][0]
        self.assertEqual(wire["expiryTime"], 1800000000000)
        self.assertEqual(wire["totalGB"], 1073741824)

    async def test_update_uses_uuid_path_but_inbound_from_model(self):
        from py3xui import Client
        client = Client(id=UUID, email="123", enable=True, inbound_id=99)
        await self.api.client.update(UUID, client)
        req = self.requests[0]
        self.assertEqual(req.url.path, f"/base/panel/api/inbounds/updateClient/{UUID}")
        body = json.loads(req.content)
        self.assertEqual(body["id"], 99)
        wire = json.loads(body["settings"])["clients"]
        self.assertEqual(len(wire), 1)
        self.assertNotIn("totalGB", wire[0])  # zero default omitted, not a partial-update guarantee

    async def test_actual_shop_update_refuses_numeric_traffic_id(self):
        from app.bot.services.vpn import VPNService
        self.reply["obj"] = {"id": 73, "uuid": UUID, "email": "123", "enable": True,
                             "inboundId": 42}
        pool = SimpleNamespace(get_connection=AsyncMock(return_value=SimpleNamespace(api=self.api)))
        service = VPNService(SimpleNamespace(xui=SimpleNamespace(INBOUND_ID=42)), None, pool)
        self.assertFalse(await service.update_client(SimpleNamespace(tg_id=123, vpn_id=UUID), 2, 30))
        self.assertEqual([r.method for r in self.requests], ["GET"])

    async def test_unknown_metadata_is_not_preserved_by_client_model(self):
        from py3xui import Client
        client = Client.model_validate({"email": "123", "enable": True,
                                       "comment": "keep", "limitHwid": 2, "uuid": UUID})
        wire = client.model_dump(by_alias=True)
        for field in ("comment", "limitHwid", "uuid"):
            self.assertNotIn(field, wire)

    async def test_shop_day_math_uses_utc_milliseconds(self):
        from app.bot.utils.time import add_days_to_timestamp
        start = 1800000000000
        self.assertEqual(add_days_to_timestamp(start, 30) - start, 30 * 86400 * 1000)

    async def test_api_instances_do_not_share_session_values(self):
        from py3xui import AsyncApi
        second = AsyncApi("https://second.invalid", "dummy", "dummy")
        self.assertNotEqual(self.api.client.session, second.client.session)
        self.assertIsNot(self.api.client, second.client)


if __name__ == "__main__":
    unittest.main()
