"""Synthetic v3.9.0 wire contracts; loopback only, no production env."""

import copy
from dataclasses import replace
import json
from pathlib import Path
import unittest

from aiohttp import web

from app.integrations.xui import XUIAdapter, XUIAuthMode, XUIClientWrite, XUIProtocolError
from app.bot.services.vpn import VPNService
from scripts import xui_live_readonly_smoke as read_smoke
from scripts import xui_live_write_smoke as write_smoke

FIXTURES = Path(__file__).parent / "fixtures/xui/v3_9_0"


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class V390WireTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.record = fixture("client.json")["obj"]
        self.inbounds = fixture("inbounds.json")
        self.response = fixture("mutation_success.json")
        self.requests = []
        self.body = None
        self.drop_after_commit = False

        async def handle(request):
            self.requests.append((request.method, request.path))
            if request.headers.get("Authorization") != "Bearer synthetic-token":
                return web.Response(status=401)
            self.assertNotIn("X-CSRF-Token", request.headers)
            path = request.path.removeprefix("/base/")
            if path == "panel/api/server/status":
                return web.json_response(fixture("server_status.json"))
            if path == "panel/api/inbounds/list":
                return web.json_response(self.inbounds)
            if path.startswith("panel/api/clients/get/"):
                if self.record is None:
                    return web.json_response({"success": False, "msg": "record not found", "obj": None})
                return web.json_response({"success": True, "obj": self.record})
            if path.startswith("panel/api/clients/traffic/"):
                return web.json_response(fixture("traffic.json"))
            if path.startswith("panel/api/clients/subLinks/"):
                return web.json_response(fixture("sub_links.json"))
            if path == "panel/api/setting/all":
                self.assertEqual(await request.json(), {})
                return web.json_response(fixture("settings.json"))
            if path == "panel/api/clients/add" or path.startswith("panel/api/clients/update/"):
                self.body = await request.json()
                previous = copy.deepcopy(self.record)
                data = self.body.get("client", self.body)
                self.record = {"client": copy.deepcopy(data), "inboundIds":
                    self.body.get("inboundIds", previous["inboundIds"] if previous else [])}
                self.record["client"]["uuid"] = self.record["client"]["id"]
                self.record["client"]["id"] = 73
                self.record["client"]["allowedIPs"] = json.dumps(data["allowedIPs"])
                # Backend emits newly added field even when input omits it.
                self.record["client"].setdefault("resetWeekday", 0)
                if self.drop_after_commit:
                    request.transport.close()
                    return web.Response()
                return web.json_response(self.response)
            if path.startswith("panel/api/clients/del/"):
                self.record = None
                return web.json_response(fixture("mutation_success.json"))
            return web.Response(status=404)

        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", handle)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/base"
        self.adapter = XUIAdapter(self.url, auth_mode=XUIAuthMode.TOKEN, token="synthetic-token")

    async def asyncTearDown(self):
        await self.adapter.close()
        await self.runner.cleanup()

    def post_count(self):
        return sum(method == "POST" and "/setting/" not in path for method, path in self.requests)

    async def test_all_read_shapes_identity_membership_traffic_and_subscription(self):
        status = await self.adapter.get_server_status()
        self.assertEqual((status.panel_version, status.xray_version), ("v3.9.0", "26.9.30"))
        inbound = await self.adapter.get_inbound(6)
        self.assertIs(inbound.raw["excludeFromSub"], False)
        self.assertEqual(VPNService._validate_provisioning_capability(inbound, None), "")
        client = await self.adapter.get_client("123")
        traffic = await self.adapter.get_client_traffic("123")
        self.assertEqual(client.inbound_ids, (6, 99))
        self.assertIsInstance(traffic.id, int)
        self.assertNotEqual(traffic.id, client.uuid)
        self.assertEqual(client.raw["resetWeekday"], 3)
        self.assertEqual(traffic.raw["resetWeekday"], 3)
        self.assertEqual(await self.adapter.get_subscription_base_url(), "https://sub.example/custom/sub/")
        links = await self.adapter.get_subscription_links(client.sub_id)
        self.assertEqual(len(links), 1)
        self.assertIn("type=xhttp", links[0])
        self.assertNotIn("xtls-rprx-vision", links[0])
        self.assertEqual(self.post_count(), 0)

    async def test_update_preserves_weekday_and_memberships_without_record_metadata(self):
        current = await self.adapter.get_client("123")
        desired = XUIClientWrite.from_client(current, expiry_time_ms=current.expiry_time_ms + 86400000,
                                            limit_ip=current.limit_ip, enable=current.enable)
        result = await self.adapter.update_client(current, desired)
        self.assertIs(result.node_pending, False)
        self.assertEqual(self.body["resetWeekday"], 3)
        self.assertEqual(self.body["reset"], 0)
        self.assertEqual(self.body["resetDay"], 0)
        self.assertEqual(result.client.inbound_ids, (6, 99))
        for key in ("updated_at", "created_at", "limitHwid", "totalGB", "subId"):
            if key.endswith("_at"):
                self.assertNotIn(key, self.body)
            else:
                self.assertEqual(self.body[key], current.raw[key])
        self.assertEqual(self.body["id"], current.uuid)
        self.assertEqual(self.post_count(), 1)

    async def test_add_normal_pending_and_lost_response_contract_no_retry(self):
        base = XUIClientWrite.from_client(await self.adapter.get_client("123"),
            expiry_time_ms=1900000000000, limit_ip=1, enable=True)
        desired = replace(base, inbound_ids=(6,))
        for kind, expected in (("null", False), ("pending", True), ("drop", None)):
            with self.subTest(kind=kind):
                self.record = None
                before = self.post_count()
                self.response = fixture("mutation_pending.json" if kind == "pending" else "mutation_success.json")
                self.drop_after_commit = kind == "drop"
                result = await self.adapter.add_client(desired)
                self.assertIs(result.node_pending, expected)
                self.assertEqual(self.post_count() - before, 1)
                self.assertEqual(self.body["inboundIds"], [6])
                self.assertEqual(self.body["client"]["flow"], "")

    async def test_invalid_weekday_fails_before_post_and_missing_old_field_is_compatible(self):
        current = await self.adapter.get_client("123")
        for value in (True, "3", -1, 8):
            with self.subTest(value=value), self.assertRaises(XUIProtocolError):
                XUIClientWrite.from_client(replace(current, raw={**current.raw, "resetWeekday": value}),
                    expiry_time_ms=1900000000000, limit_ip=1, enable=True)
        raw = dict(current.raw)
        del raw["resetWeekday"]
        desired = XUIClientWrite.from_client(replace(current, raw=raw),
            expiry_time_ms=1900000000000, limit_ip=1, enable=True)
        self.assertNotIn("resetWeekday", desired.client_payload())
        self.assertEqual(self.post_count(), 0)

    async def test_new_weekday_change_participates_in_stale_snapshot_guard(self):
        from app.integrations.xui import XUIAmbiguousWriteError
        current = await self.adapter.get_client("123")
        desired = XUIClientWrite.from_client(current, expiry_time_ms=1900000000000,
                                            limit_ip=1, enable=True)
        self.record["client"]["resetWeekday"] = 5
        with self.assertRaises(XUIAmbiguousWriteError):
            await self.adapter.update_client(current, desired)
        self.assertEqual(self.post_count(), 0)

    async def test_delete_acknowledges_persistence_not_pending_activation(self):
        config = read_smoke.SmokeConfig(self.url, "synthetic-token", 6)
        state = write_smoke.SmokeState.generate(config)
        self.record["client"].update(email=state.email, uuid=state.uuid, subId=state.sub_id)
        self.record["inboundIds"] = [6]
        async with write_smoke.WriteSmokeAdapter(config, state, "cleanup") as adapter:
            result = await adapter.cleanup_test_client()
            self.assertTrue(result["persistence_deleted"])
            self.assertEqual(result["activation"], "UNKNOWN")
            self.assertEqual(adapter.mutation_response_metadata.obj_shape, "null")
            self.assertTrue(adapter.mutation_response_metadata.valid_mutation_response)
            self.assertEqual(adapter.mutation_count, 1)

    async def test_prepared_v390_read_smoke_explicit_version_xhttp_and_visibility(self):
        config = read_smoke.SmokeConfig(self.url, "synthetic-token", 6)
        result = await read_smoke.run_smoke(config, dry_run=True, expected_panel_version="3.9.0")
        self.assertEqual(result["network_requests"], 0)
        self.assertEqual(self.requests, [])
        result = await read_smoke.run_smoke(config, dry_run=False, expected_panel_version="3.9.0")
        self.assertEqual(result["outcome"], "PASS")
        self.assertEqual(result["inbound"]["resolved_flow"], "")
        self.assertEqual(result["network_requests"], 2)
        self.assertEqual(self.post_count(), 0)
        self.inbounds["obj"][0]["excludeFromSub"] = True
        result = await read_smoke.run_smoke(config, dry_run=False, expected_panel_version="3.9.0")
        self.assertEqual(result["outcome"], "FAIL")
        self.assertEqual(self.post_count(), 0)
