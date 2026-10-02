"""Synthetic loopback HTTP only. Production .env is never read by these tests."""

import asyncio
import contextlib
import copy
from dataclasses import replace
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import xui_live_write_smoke as smoke


class SmokeJournalTests(unittest.TestCase):
    def setUp(self):
        self.config = smoke.SmokeConfig("https://panel.example/base", "secret-api-token", 6)

    def test_numeric_unsafe_email_wrong_uuid_subid_and_inbound_rejected(self):
        state = smoke.SmokeState.generate(self.config)
        for changes in ({"email": "123456789"}, {"email": "shop-smoke-../other"},
                        {"email": "not-a-test"}, {"inbound_id": 42},
                        {"uuid": "bad"}, {"sub_id": str(smoke.uuid4())}):
            with self.subTest(changes=changes), self.assertRaises(smoke.SmokeError):
                replace(state, **changes).validate(self.config)
        with self.assertRaises(smoke.SmokeError):
            state.validate(replace(self.config, inbound_id=42))
        with self.assertRaises(smoke.SmokeError):
            state.validate(replace(self.config, host="https://another-panel.example/base"))

    def test_state_roundtrip_no_token_or_links_and_corruption_never_regenerates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            state = smoke.SmokeState.generate(self.config)
            smoke.save_state(path, state)
            self.assertEqual(smoke.load_state(path, self.config), state)
            text = path.read_text()
            for secret in (self.config.token, "Bearer", "https://", "vless://", "cookie", "csrf"):
                self.assertNotIn(secret, text)
            for value in ("not JSON", json.dumps({**smoke.asdict(state), "token": "secret"})):
                path.write_text(value)
                with self.assertRaises(smoke.SmokeError):
                    smoke.load_state(path, self.config)
            path.unlink()
            with self.assertRaises(smoke.SmokeError):
                smoke.load_state(path, self.config)

    def test_exclusive_lock_blocks_second_process_and_stale_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / "state.lock"
            with smoke.exclusive_run(lock):
                with self.assertRaises(smoke.SmokeError):
                    with smoke.exclusive_run(lock):
                        self.fail("second run must not enter")
            self.assertFalse(lock.exists())

    def test_dry_run_real_cli_zero_network_and_redacted_output(self):
        env = {"XUI_HOST": self.config.host, "XUI_API_TOKEN": self.config.token,
               "XUI_INBOUND_ID": "6", "XUI_AUTH_MODE": "token"}
        def git_result(args, **kwargs):
            return subprocess.CompletedProcess(args, 0 if "check-ignore" in args else 1)
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(smoke, "STATE_PATH", Path(directory) / "state.json"), \
                patch.object(smoke, "LOCK_PATH", Path(directory) / "state.lock"), \
                patch.object(smoke, "load_local_env", return_value=env), \
                patch.object(smoke.subprocess, "run", side_effect=git_result), \
                patch.object(smoke.aiohttp, "ClientSession") as session, \
                patch.object(smoke, "logging"), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(smoke.main(["--dry-run"]), 0)
            state = smoke.load_state(smoke.STATE_PATH, self.config)
            session.assert_not_called()
            summary = json.loads(output.getvalue())
            self.assertEqual(summary["network_requests"], 0)
            self.assertEqual(summary["intended"]["inboundIds"], [6])
            for secret in (self.config.token, state.uuid, state.sub_id, "Bearer", "vless://"):
                self.assertNotIn(secret, output.getvalue())
            output.truncate(0)
            output.seek(0)
            self.assertEqual(smoke.main(["--create"]), 1)
            session.assert_not_called()

    def test_share_links_validate_uuid_transport_security_and_empty_flow(self):
        state = smoke.SmokeState.generate(self.config)
        link = f"vless://{state.uuid}@example.test:443?type=xhttp&security=reality"
        self.assertEqual(smoke.verify_links([link], state), 1)
        for wrong in (link + "&flow=xtls-rprx-vision", link.replace("xhttp", "tcp"),
                      link.replace("reality", "tls"), link.replace(state.uuid, "other")):
            with self.subTest(link="redacted"), self.assertRaises(smoke.SmokeError):
                smoke.verify_links([wrong], state)

    def test_unsafe_cli_config_wrong_inbound_and_session_rejected_without_network(self):
        for change in ({"XUI_INBOUND_ID": "42"}, {"XUI_AUTH_MODE": "session"}):
            env = {"XUI_HOST": self.config.host, "XUI_API_TOKEN": self.config.token,
                   "XUI_INBOUND_ID": "6", "XUI_AUTH_MODE": "token", **change}
            with self.subTest(change=change), patch.object(smoke, "load_local_env", return_value=env), \
                    patch.object(smoke, "logging"), patch.object(smoke.aiohttp, "ClientSession") as session, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(smoke.main(["--dry-run"]), 1)
                session.assert_not_called()


class OptionalSubscriptionTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_subscription_plain_and_base64_without_bearer_or_cookies(self):
        from types import SimpleNamespace
        config = smoke.SmokeConfig("https://panel.example/base", "must-not-be-forwarded", 6)
        state = smoke.SmokeState.generate(config)
        link = f"vless://{state.uuid}@example.test:443?type=xhttp&security=reality"
        calls = []
        class Response:
            status = 200
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                return False
            @property
            def content(self):
                return self
            async def iter_chunked(self, size):
                yield body[:10]
                yield body[10:]
        class Session:
            def __init__(self, **kwargs):
                self_options = kwargs
                self_options.pop("timeout")
                self_options.pop("cookie_jar")
                assert self_options == {"trust_env": False}
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                return False
            def get(self, url, **kwargs):
                calls.append((url, kwargs))
                return Response()
        for body in (link.encode(), smoke.base64.b64encode(link.encode())):
            adapter = SimpleNamespace(_timeout=smoke.aiohttp.ClientTimeout(total=1), subscription_requests=0)
            with patch.object(smoke.aiohttp, "ClientSession", Session):
                self.assertEqual(await smoke.fetch_subscription(adapter, "https://sub.example/custom/", state), 1)
            self.assertEqual(adapter.subscription_requests, 1)
        self.assertEqual(len(calls), 2)
        for url, kwargs in calls:
            self.assertEqual(url, "https://sub.example/custom/" + state.sub_id)
            self.assertEqual(kwargs, {"allow_redirects": False})
            self.assertNotIn(config.token, str(kwargs))


class LiveWriteWireTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from aiohttp import web

        self.inbound = json.loads((Path(__file__).parent / "fixtures/xui/v3_8_5/inbound_xhttp.json").read_text())
        self.record = None
        self.memberships = [6]
        self.physical = []
        self.behavior = "normal"
        self.pending = False
        self.panel_version = "3.8.5"
        self.created_snapshot = None

        async def handler(request):
            self.physical.append((request.method, request.path, dict(request.headers)))
            if request.headers.get("Authorization") != "Bearer synthetic-admin-token":
                return web.Response(status=401)
            path = request.path.removeprefix("/base/")
            if path == "panel/api/server/status":
                return web.json_response({"success": True, "obj": {
                    "panelVersion": self.panel_version, "xray": {"version": "26.9.30", "state": "running"}}})
            if path == "panel/api/inbounds/list":
                return web.json_response({"success": True, "obj": [self.inbound]})
            if path == f"panel/api/clients/get/{self.state.email}":
                if self.behavior == "missing_route":
                    return web.Response(status=404)
                if self.behavior == "drop_get":
                    request.transport.close()
                    return web.Response()
                if self.record is None:
                    return web.json_response({"success": False, "msg": "record not found", "obj": None})
                return web.json_response({"success": True, "obj": {
                    "client": self.record, "inboundIds": self.memberships}})
            if path == f"panel/api/clients/traffic/{self.state.email}":
                return web.json_response({"success": True, "obj": {
                    "id": 12345, "inboundId": 6, "email": self.state.email, "uuid": "not-credential-identity",
                    "up": 0, "down": 0, "total": 0, "expiryTime": self.record["expiryTime"], "enable": True}})
            if path == "panel/api/setting/all":
                return web.json_response({"success": True, "obj": {"subURI": "https://sub.example/synthetic/"}})
            if path == f"panel/api/clients/subLinks/{self.state.sub_id}":
                return web.json_response({"success": True, "obj": [
                    f"vless://{self.state.uuid}@example.test:443?type=xhttp&security=reality&path=%2Fsynthetic"]})
            mutation = path in ("panel/api/clients/add", f"panel/api/clients/update/{self.state.email}",
                                f"panel/api/clients/del/{self.state.email}")
            if mutation:
                if path.endswith("/add"):
                    body = await request.json()
                    self.last_body = body
                    if self.behavior != "timeout_old":
                        self.store(body["client"])
                        self.memberships = body["inboundIds"]
                        self.created_snapshot = copy.deepcopy(self.record)
                elif "/update/" in path:
                    body = await request.json()
                    self.last_body = body
                    if self.behavior != "timeout_old":
                        self.store(body)
                elif self.behavior != "timeout_old":
                    self.record = None
                if self.behavior in ("timeout_commit", "timeout_old"):
                    await asyncio.sleep(1.5)
                if self.behavior == "malformed":
                    return web.Response(text="invalid JSON")
                if self.behavior == "drop_commit":
                    request.transport.close()
                    return web.Response()
                if self.behavior == "unknown_obj":
                    return web.json_response({"success": True, "obj": {}})
                if self.behavior == "rejected":
                    return web.json_response({"success": False, "obj": None})
                if "/del/" in path:
                    return web.json_response({"success": True, "msg": "deleted", "obj": None})
                return web.json_response({"success": True, "obj": {"nodePending": True} if self.pending else None})
            return web.Response(status=404)

        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", handler)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.config = smoke.SmokeConfig(f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/base",
                                        "synthetic-admin-token", 6)
        self.state = smoke.SmokeState.generate(self.config)
        self.journal = []

    async def asyncTearDown(self):
        await self.runner.cleanup()

    def store(self, payload):
        self.record = copy.deepcopy(payload)
        self.record["uuid"] = self.record.pop("id")
        self.record["id"] = 987
        self.record["allowedIPs"] = json.dumps(self.record["allowedIPs"])

    def persist(self, state):
        self.state = state
        self.journal.append(state.stage)

    def adapter_factory(self, config, state, stage):
        adapter = smoke.WriteSmokeAdapter(config, state, stage)
        adapter._timeout = smoke.aiohttp.ClientTimeout(total=1)
        return adapter

    async def run_stage(self, stage):
        return await smoke.run_stage(self.config, self.state, stage, self.persist,
                                     adapter_factory=self.adapter_factory)

    def mutations(self):
        return [r for r in self.physical if r[0] == "POST" and not r[1].endswith("setting/all")]

    async def prepare_and_create(self):
        self.assertEqual((await self.run_stage("preflight"))["outcome"], "PASS")
        self.assertEqual((await self.run_stage("create"))["outcome"], "PASS")

    async def test_preflight_only_exact_reads_and_durable_journal(self):
        result = await self.run_stage("preflight")
        self.assertEqual(result["outcome"], "PASS")
        self.assertEqual(result["panel_requests"], 3)
        self.assertEqual(result["mutations"], 0)
        self.assertEqual([r[:2] for r in self.physical], [
            ("GET", "/base/panel/api/server/status"), ("GET", "/base/panel/api/inbounds/list"),
            ("GET", f"/base/panel/api/clients/get/{self.state.email}")])
        self.assertEqual(self.state.stage, "PREFLIGHT_PASS")

    async def test_null_success_safe_metadata_and_cleanup_contract(self):
        await self.run_stage("preflight")
        result = await self.run_stage("create")
        self.assertEqual(result["outcome"], "PASS")
        info = result["mutation_response_metadata"]
        self.assertEqual(info, {"response_received": True, "http_status": 200,
            "envelope_valid": True, "success": True, "obj_shape": "null",
            "node_pending_present": False, "node_pending_value": None,
            "valid_mutation_response": True})
        for secret in (self.config.token, self.state.uuid, self.state.sub_id):
            self.assertNotIn(secret, json.dumps(info))
        result = await self.run_stage("cleanup")
        self.assertEqual(result["outcome"], "PASS")
        self.assertEqual(result["mutation_response_metadata"], info)
        self.assertEqual(result["cleanup"]["activation"], "UNKNOWN")

    async def test_delete_unknown_or_error_response_does_not_claim_activation(self):
        for behavior in ("unknown_obj", "malformed", "rejected", "timeout_commit", "drop_commit"):
            with self.subTest(behavior=behavior):
                self.behavior = "normal"
                self.state = replace(self.state, stage="PREFLIGHT_PASS")
                self.record = None
                self.assertEqual((await self.run_stage("create"))["outcome"], "PASS")
                self.behavior = behavior
                start = len(self.mutations())
                result = await self.run_stage("cleanup")
                self.assertEqual(len(self.mutations()) - start, 1)
                self.assertEqual(result["cleanup"]["activation"], "UNKNOWN")
                self.assertFalse(result["mutation_response_metadata"]["valid_mutation_response"])

    async def test_wrong_version_or_disabled_inbound_stops_preflight(self):
        self.panel_version = "3.9.0"
        self.assertEqual((await self.run_stage("preflight"))["outcome"], "FAIL")
        self.assertEqual(len(self.physical), 1)
        self.panel_version = "3.8.5"
        self.inbound["enable"] = False
        self.assertEqual((await self.run_stage("preflight"))["outcome"], "FAIL")
        self.assertEqual(self.mutations(), [])

    async def test_existing_test_email_blocks_preflight_and_create(self):
        self.store(self.state.create_request().client_payload())
        self.assertEqual((await self.run_stage("preflight"))["outcome"], "FAIL")
        self.state = replace(self.state, stage="PREFLIGHT_PASS")
        self.assertEqual((await self.run_stage("create"))["outcome"], "FAIL")
        self.assertEqual(self.mutations(), [])

    async def test_restart_with_attempted_journal_cannot_replay_create(self):
        self.state = replace(self.state, stage="CREATE_ATTEMPTED")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            smoke.save_state(path, self.state)
            self.state = smoke.load_state(path, self.config)
        with self.assertRaises(smoke.SmokeError):
            await self.run_stage("create")
        self.assertEqual(self.physical, [])

    async def test_durable_journal_failure_before_mutation_blocks_post(self):
        self.state = replace(self.state, stage="PREFLIGHT_PASS")
        def broken_persist(state):
            raise OSError("synthetic journal write failure")
        result = await smoke.run_stage(self.config, self.state, "create", broken_persist,
                                       adapter_factory=self.adapter_factory)
        self.assertEqual(result["outcome"], "FAIL")
        self.assertEqual(result["mutations"], 0)
        self.assertEqual(self.mutations(), [])

    async def test_journal_completion_failure_reports_counts_and_blocks_replay(self):
        self.state = replace(self.state, stage="PREFLIGHT_PASS")
        def fail_after_intent(state):
            if state.stage == "CREATE_ATTEMPTED":
                self.persist(state)
            else:
                raise OSError("synthetic completion failure")
        result = await smoke.run_stage(self.config, self.state, "create", fail_after_intent,
                                       adapter_factory=self.adapter_factory)
        self.assertEqual(result["outcome"], "FAIL")
        self.assertEqual(result["mutations"], 1)
        self.assertEqual(self.state.stage, "CREATE_ATTEMPTED")
        self.assertEqual(len(self.mutations()), 1)
        with self.assertRaises(smoke.SmokeError):
            await self.run_stage("create")

    async def test_guard_blocks_other_client_reads_and_cleanup_without_arming(self):
        async with self.adapter_factory(self.config, self.state, "cleanup") as adapter:
            for method, endpoint in (("GET", "panel/api/clients/get/123"),
                    ("POST", "panel/api/clients/del/123"),
                    ("POST", f"panel/api/clients/del/{self.state.email}"),
                    ("POST", "panel/api/setting/all"), ("GET", "csrf-token")):
                with self.subTest(endpoint=endpoint), self.assertRaises(smoke.SmokeError):
                    await adapter._request(method, endpoint)
        self.assertEqual(self.physical, [])

    async def test_missing_route_is_not_assumed_client_absence(self):
        self.behavior = "missing_route"
        self.assertEqual((await self.run_stage("preflight"))["outcome"], "FAIL")
        self.assertEqual(self.state.stage, "PREPARED")
        self.assertEqual(self.mutations(), [])

    async def test_disconnected_get_has_no_hidden_aiohttp_retry(self):
        self.behavior = "drop_get"
        self.assertEqual((await self.run_stage("preflight"))["outcome"], "FAIL")
        self.assertEqual(sum("/get/" in r[1] for r in self.physical), 1)

    async def test_create_one_post_empty_flow_membership_and_intent_before_network(self):
        await self.prepare_and_create()
        self.assertEqual(len(self.mutations()), 1)
        self.assertEqual(self.last_body["client"]["flow"], "")
        self.assertEqual(self.last_body["inboundIds"], [6])
        self.assertEqual(self.last_body["client"]["limitIp"], 1)
        self.assertEqual(self.journal, ["PREFLIGHT_PASS", "CREATE_ATTEMPTED", "CREATE_PASS"])
        with self.assertRaises(smoke.SmokeError):
            await self.run_stage("create")
        self.assertEqual(len(self.mutations()), 1)

    async def test_create_timeout_after_commit_requires_review_without_second_post(self):
        await self.run_stage("preflight")
        self.behavior = "timeout_commit"
        result = await self.run_stage("create")
        self.assertEqual(result["outcome"], "FAIL")
        self.assertEqual(result["mutations"], 1)
        self.assertIsNone(result["node_pending"])
        self.assertEqual(self.state.stage, "CREATE_REVIEW")
        self.assertEqual(len(self.mutations()), 1)
        with self.assertRaises(smoke.SmokeError):
            await self.run_stage("create")

    async def test_create_timeout_without_commit_requires_review_without_retry(self):
        await self.run_stage("preflight")
        self.behavior = "timeout_old"
        self.assertEqual((await self.run_stage("create"))["outcome"], "FAIL")
        self.assertIsNone(self.record)
        self.assertEqual(self.state.stage, "CREATE_REVIEW")
        self.assertEqual(len(self.mutations()), 1)

    async def test_node_pending_and_malformed_response_cannot_promote_activation_by_get(self):
        for behavior, pending in (("normal", True), ("malformed", False)):
            self.state = smoke.SmokeState.generate(self.config)
            self.record = None
            self.behavior, self.pending = behavior, pending
            with self.subTest(behavior=behavior):
                await self.run_stage("preflight")
                self.assertEqual((await self.run_stage("create"))["outcome"], "FAIL")
                self.assertEqual(self.state.stage, "CREATE_REVIEW")
                self.assertEqual((await self.run_stage("verify-create"))["outcome"], "FAIL")
                self.assertEqual(self.state.stage, "CREATE_REVIEW")

    async def test_verify_create_read_only_traffic_not_identity_and_no_secret_output(self):
        await self.prepare_and_create()
        before = len(self.mutations())
        result = await self.run_stage("verify-create")
        self.assertEqual(result["outcome"], "PASS")
        self.assertTrue(result["traffic_read"])
        self.assertEqual(result["share_links_verified"], 1)
        self.assertEqual(result["subscription_requests"], 0)
        self.assertEqual(len(self.mutations()), before)
        for secret in (self.config.token, self.state.uuid, self.state.sub_id, "vless://", "Bearer"):
            self.assertNotIn(secret, json.dumps(result))

    async def test_update_exactly_one_post_changes_only_expiry(self):
        await self.prepare_and_create()
        await self.run_stage("verify-create")
        before = copy.deepcopy(self.record)
        count = len(self.mutations())
        result = await self.run_stage("update")
        self.assertEqual(result["outcome"], "PASS")
        self.assertEqual(len(self.mutations()), count + 1)
        expected = dict(before, expiryTime=self.state.updated_expiry)
        self.assertEqual(self.record, expected)
        self.assertEqual(self.memberships, [6])
        self.assertEqual((await self.run_stage("verify-update"))["outcome"], "PASS")
        self.assertEqual(len(self.mutations()), count + 1)

    async def test_update_external_preservation_edit_blocks_post(self):
        await self.prepare_and_create()
        await self.run_stage("verify-create")
        self.record["comment"] = "external change"
        self.assertEqual((await self.run_stage("update"))["outcome"], "FAIL")
        self.assertEqual(len(self.mutations()), 1)

    async def test_update_timeout_no_retry_and_review(self):
        await self.prepare_and_create()
        await self.run_stage("verify-create")
        self.behavior = "timeout_commit"
        result = await self.run_stage("update")
        self.assertEqual(result["outcome"], "FAIL")
        self.assertEqual(self.state.stage, "UPDATE_REVIEW")
        self.assertEqual(len(self.mutations()), 2)
        with self.assertRaises(smoke.SmokeError):
            await self.run_stage("update")

    async def test_cleanup_uuid_subid_and_membership_mismatch_refuses_delete(self):
        await self.prepare_and_create()
        original = copy.deepcopy(self.record)
        for field, value in (("uuid", str(smoke.uuid4())), ("subId", str(smoke.uuid4()))):
            self.record = dict(original, **{field: value})
            self.assertEqual((await self.run_stage("cleanup"))["outcome"], "FAIL")
        self.record = original
        self.memberships = [6, 99]
        self.assertEqual((await self.run_stage("cleanup"))["outcome"], "FAIL")
        self.assertEqual(len(self.mutations()), 1)

    async def test_cleanup_exact_modern_post_no_body_and_activation_unknown(self):
        await self.prepare_and_create()
        result = await self.run_stage("cleanup")
        self.assertEqual(result["outcome"], "PASS")
        self.assertEqual(result["mutations"], 1)
        self.assertEqual(result["cleanup"]["activation"], "UNKNOWN")
        self.assertEqual(self.mutations()[-1][:2], ("POST", f"/base/panel/api/clients/del/{self.state.email}"))
        self.assertIsNone(self.record)
        self.assertEqual((await self.run_stage("verify-cleanup"))["outcome"], "PASS")

    async def test_ambiguous_cleanup_after_commit_no_retry(self):
        await self.prepare_and_create()
        self.behavior = "timeout_commit"
        result = await self.run_stage("cleanup")
        self.assertEqual(result["outcome"], "PASS")
        self.assertEqual(result["cleanup"]["mutation_response"], "UNKNOWN")
        self.assertEqual(result["cleanup"]["activation"], "UNKNOWN")
        self.assertEqual(len(self.mutations()), 2)
        with self.assertRaises(smoke.SmokeError):
            await self.run_stage("cleanup")

    async def test_ambiguous_cleanup_without_commit_manual_review_no_retry(self):
        await self.prepare_and_create()
        self.behavior = "timeout_old"
        self.assertEqual((await self.run_stage("cleanup"))["outcome"], "FAIL")
        self.assertEqual(self.state.stage, "CLEANUP_REVIEW")
        with self.assertRaises(smoke.SmokeError):
            await self.run_stage("cleanup")
        self.assertEqual(len(self.mutations()), 2)

    async def test_guard_unknown_write_wrong_identity_body_and_second_post_blocked(self):
        async with self.adapter_factory(self.config, self.state, "create") as adapter:
            for endpoint in ("login", "panel/api/clients/update/other", "panel/api/setting/update", "unknown"):
                with self.assertRaises(smoke.SmokeError):
                    await adapter._request("POST", endpoint)
            with self.assertRaises(smoke.SmokeError):
                await adapter._request("POST", "panel/api/clients/add")
            expected = {"client": self.state.create_request().client_payload(), "inboundIds": [6]}
            adapter.arm_mutation(expected)
            with self.assertRaises(smoke.SmokeError):
                await adapter._request("POST", "panel/api/clients/add", json_body={})
            await adapter._request("POST", "panel/api/clients/add", json_body=expected, mutation=True)
            with self.assertRaises(smoke.SmokeError):
                await adapter._request("POST", "panel/api/clients/add", json_body=expected, mutation=True)
        self.assertEqual(len(self.mutations()), 1)

    async def test_full_staged_lifecycle_fake_server_and_no_secrets_in_logs(self):
        import logging
        with patch.object(logging.Logger, "_log") as logs:
            for stage in ("preflight", "create", "verify-create", "update", "verify-update", "cleanup", "verify-cleanup"):
                with self.subTest(stage=stage):
                    result = await self.run_stage(stage)
                    self.assertEqual(result["outcome"], "PASS")
            output = str(logs.call_args_list)
            for secret in (self.config.token, self.state.uuid, self.state.sub_id, "vless://", "Bearer"):
                self.assertNotIn(secret, output)
        self.assertEqual(self.state.stage, "VERIFY_CLEANUP_PASS")
        self.assertEqual(len(self.mutations()), 3)
        self.assertIsNone(self.record)
