"""Manual staged smoke for one synthetic client; never used by bot startup."""

from __future__ import annotations

import argparse
import asyncio
import base64
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Callable
from unittest.mock import patch
from urllib.parse import quote, parse_qs, urlsplit
from uuid import UUID, uuid4

import aiohttp

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.bot.services.vpn import VPNService
from app.integrations.xui import (
    XUIAdapter, XUIAPIError, XUIAuthMode, XUIClientWrite, XUIError,
    XUINotFoundError, XUIProtocolError, XUITransportError,
)
from scripts.xui_live_readonly_smoke import SmokeConfig, SmokeError, load_local_env, redact
from app.integrations.xui.models import XUIMutationResponseMetadata

STATE_PATH = ROOT / ".xui-write-smoke-state.json"
LOCK_PATH = ROOT / ".xui-write-smoke-state.lock"
OUTCOME_PATH = ROOT / ".xui-write-smoke-outcome.json"
DAY_MS = 86400000
STAGES = ("dry-run", "preflight", "create", "verify-create", "update",
          "verify-update", "cleanup", "verify-cleanup")
STATES = frozenset({"PREPARED", "PREFLIGHT_PASS", "CREATE_ATTEMPTED", "CREATE_PASS",
    "CREATE_REVIEW", "VERIFY_CREATE_PASS", "UPDATE_ATTEMPTED", "UPDATE_PASS",
    "UPDATE_REVIEW", "VERIFY_UPDATE_PASS", "CLEANUP_ATTEMPTED", "CLEANUP_REVIEW",
    "CLEANUP_PERSISTED", "VERIFY_CLEANUP_PASS"})


def host_binding(config: SmokeConfig) -> str:
    return hashlib.sha256(config.host.rstrip("/").encode()).hexdigest()


@dataclass(frozen=True)
class SmokeState:
    email: str
    uuid: str = field(repr=False)
    sub_id: str = field(repr=False)
    inbound_id: int
    initial_expiry: int
    updated_expiry: int
    host_digest: str
    stage: str = "PREPARED"
    preservation_digest: str = ""

    def validate(self, config: SmokeConfig) -> None:
        if config.inbound_id != 6 or type(self.inbound_id) is not int or self.inbound_id != 6:
            raise SmokeError("This smoke is restricted to inbound 6")
        if not isinstance(self.email, str) or not re.fullmatch(r"shop-smoke-\d{14}-[0-9a-f]{8}", self.email):
            raise SmokeError("Unsafe smoke email; exact synthetic prefix is required")
        try:
            valid = all(str(UUID(value)) == value and UUID(value).version == 4
                        for value in (self.uuid, self.sub_id))
        except (ValueError, TypeError, AttributeError):
            valid = False
        if not valid or self.sub_id != self.uuid or not self.email.endswith(self.uuid[:8]):
            raise SmokeError("Invalid smoke credential identity")
        if (type(self.initial_expiry) is not int or type(self.updated_expiry) is not int
                or self.initial_expiry <= 0 or self.updated_expiry != self.initial_expiry + DAY_MS
                or self.stage not in STATES or self.host_digest != host_binding(config)
                or not isinstance(self.preservation_digest, str)
                or (self.preservation_digest and not re.fullmatch(r"[0-9a-f]{64}", self.preservation_digest))):
            raise SmokeError("Invalid smoke journal or panel binding")

    @classmethod
    def generate(cls, config: SmokeConfig) -> SmokeState:
        credential = str(uuid4())
        now = datetime.now(timezone.utc)
        state = cls(f"shop-smoke-{now:%Y%m%d%H%M%S}-{credential[:8]}", credential,
                    credential, 6, int(now.timestamp() * 1000) + DAY_MS,
                    int(now.timestamp() * 1000) + 2 * DAY_MS, host_binding(config))
        state.validate(config)
        return state

    def create_request(self) -> XUIClientWrite:
        return XUIClientWrite(email=self.email, uuid=self.uuid, inbound_ids=(6,),
            expiry_time_ms=self.initial_expiry, total_bytes=0, limit_ip=1,
            limit_hwid=0, tg_id=0, sub_id=self.sub_id, flow="", enable=True)


def save_state(path: Path, state: SmokeState) -> None:
    """Durable intent before HTTP. Atomic replacement; no provider secrets stored."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as file:
        json.dump(asdict(state), file, sort_keys=True)
        file.flush()
        os.fsync(file.fileno())
    os.replace(tmp, path)


def load_state(path: Path, config: SmokeConfig) -> SmokeState:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or set(value) != set(SmokeState.__dataclass_fields__):
            raise ValueError()
        state = SmokeState(**value)
        state.validate(config)
        return state
    except (OSError, ValueError, TypeError):
        raise SmokeError("Smoke state missing/corrupt; identity will not be guessed") from None


@contextmanager
def exclusive_run(path: Path):
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise SmokeError("Another run or stale lock exists; inspect journal manually") from None
    try:
        os.close(descriptor)
        yield
    finally:
        path.unlink()


def plan(config: SmokeConfig, state: SmokeState, stage: str) -> list[tuple[str, str]]:
    reads = [("GET", "panel/api/server/status"), ("GET", "panel/api/inbounds/list"),
             ("GET", f"panel/api/clients/get/{quote(state.email, safe='')}")]
    if stage in ("verify-create", "verify-update"):
        reads += [("GET", f"panel/api/clients/traffic/{state.email}"),
                  ("POST", "panel/api/setting/all"),
                  ("GET", f"panel/api/clients/subLinks/{state.sub_id}")]
    mutation = {"create": "panel/api/clients/add", "update": f"panel/api/clients/update/{state.email}",
                "cleanup": f"panel/api/clients/del/{state.email}"}.get(stage)
    return reads + ([("POST", mutation)] if mutation else [])


def planned_summary(config: SmokeConfig, state: SmokeState) -> dict:
    prefix = urlsplit(config.host).path.rstrip("/")
    return {"outcome": "PREPARED", "network_requests": 0,
        "preflight": [{"method": method, "path": prefix + "/" + endpoint}
                      for method, endpoint in plan(config, state, "preflight")],
        "mutations": {stage: {"method": "POST", "path": prefix + "/" + plan(config, state, stage)[-1][1]}
                      for stage in ("create", "update", "cleanup")},
        "intended": {"email": state.email, "uuid": redact(state.uuid), "subId": redact(state.sub_id),
                     "inboundIds": [6], "expiry_utc": datetime.fromtimestamp(
                         state.initial_expiry / 1000, timezone.utc).isoformat(),
                     "updated_expiry_utc": datetime.fromtimestamp(
                         state.updated_expiry / 1000, timezone.utc).isoformat(),
                     "flow": "", "enable": True, "limitIp": 1, "limitHwid": 0, "totalGB": 0},
        "journal_stage": state.stage, "required_token_scope": "admin"}


def assert_identity(client, state: SmokeState) -> None:
    if (client.email != state.email or client.uuid != state.uuid or client.sub_id != state.sub_id
            or client.inbound_ids != (6,)):
        raise SmokeError("Smoke identity/membership mismatch; mutation blocked")


def preservation_digest(client) -> str:
    payload = XUIClientWrite.from_client(client, expiry_time_ms=client.expiry_time_ms,
                                         limit_ip=client.limit_ip, enable=client.enable).client_payload()
    del payload["expiryTime"]
    value = {"client": payload, "inboundIds": list(client.inbound_ids)}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class WriteSmokeAdapter(XUIAdapter):
    """Exact identity/stage allowlist, one mutation maximum, no hidden GET retries."""

    def __init__(self, config: SmokeConfig, state: SmokeState, stage: str):
        state.validate(config)
        if stage not in STAGES or stage == "dry-run":
            raise SmokeError("Invalid network stage")
        self.state, self.stage = state, stage
        self.allowed = frozenset(plan(config, state, stage))
        self.requests = []
        self.mutation_count = 0
        self.expected_body = None
        self._mutation_armed = False
        self.subscription_requests = 0
        self.mutation_response_metadata = None
        super().__init__(config.host, auth_mode=XUIAuthMode.TOKEN, token=config.token, timeout_seconds=10)

    def _get_session(self):
        if self._closed:
            raise SmokeError("Smoke adapter is closed")
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=self._timeout,
                cookie_jar=aiohttp.DummyCookieJar(), trust_env=False)
        return self._session

    async def _request(self, method, endpoint, **kwargs):
        if (method, endpoint) not in self.allowed or kwargs.get("login") or kwargs.get("csrf_bootstrap"):
            raise SmokeError("Request blocked by exact stage allowlist")
        is_mutation = method == "POST" and endpoint != "panel/api/setting/all"
        body = kwargs.get("json_body")
        if is_mutation:
            if not self._mutation_armed or self.mutation_count or body != self.expected_body:
                raise SmokeError("Mutation count/body guard blocked request")
            self.mutation_count += 1
        elif method == "POST" and body != {}:
            raise SmokeError("Read-only settings call must have empty body")
        if not hasattr(aiohttp.client, "IDEMPOTENT_METHODS"):
            raise SmokeError("Unsupported aiohttp retry policy")
        self.requests.append((method, endpoint))
        if is_mutation:
            metadata = kwargs.get("response_metadata") or XUIMutationResponseMetadata()
            kwargs["response_metadata"] = metadata
            self.mutation_response_metadata = metadata
        with patch.object(aiohttp.client, "IDEMPOTENT_METHODS", frozenset()):
            if self.stage == "cleanup" and is_mutation:
                return await self._delete_response(endpoint, metadata)
            return await super()._request(method, endpoint, **kwargs)

    def arm_mutation(self, body):
        """Called only after durable intent and fresh identity/capability checks."""
        self.expected_body = body
        self._mutation_armed = True

    async def get_client(self, email):
        try:
            return await super().get_client(email)
        except XUINotFoundError as error:
            # A missing route (HTTP 404) is not proof that the email is unused.
            if str(error) != "Panel client not found":
                raise SmokeError("Canonical endpoint unavailable; cannot establish absence") from None
            raise

    async def _delete_response(self, endpoint, metadata):
        # v3.8.5 delete uses jsonMsg -> obj=null, not pendingNodeObj.
        # Runtime adapter deliberately exposes no generic deletion operation.
        try:
            async with self._get_session().request("POST", self._url(endpoint),
                    headers={"Authorization": f"Bearer {self._token}"}, allow_redirects=False) as response:
                metadata.response_received = True
                metadata.http_status = response.status
                if not 200 <= response.status < 300:
                    raise XUITransportError("Cleanup HTTP response failed")
                try:
                    payload = await response.json(content_type=None)
                    metadata.inspect_envelope(payload)
                except ValueError:
                    raise XUIProtocolError("Cleanup response malformed") from None
                if not metadata.envelope_valid:
                    raise XUIProtocolError("Cleanup envelope malformed")
                if metadata.success is not True:
                    raise XUIAPIError("Cleanup API rejected request")
                if payload["obj"] is not None:
                    raise XUIProtocolError("Unexpected cleanup response object")
                metadata.valid_mutation_response = True
        except (aiohttp.ClientError, asyncio.TimeoutError):
            raise XUITransportError("Cleanup transport outcome unknown") from None

    async def cleanup_test_client(self):
        current = await self.get_client(self.state.email)
        assert_identity(current, self.state)
        self.arm_mutation(None)
        error = False
        try:
            await self._request("POST", f"panel/api/clients/del/{self.state.email}", mutation=True)
        except XUIError:
            error = True
        try:
            await self.get_client(self.state.email)
        except XUINotFoundError:
            return {"persistence_deleted": True, "activation": "UNKNOWN",
                    "mutation_response": "UNKNOWN" if error else "SUCCESS"}
        raise SmokeError("Cleanup not confirmed; manual review, never retry")


async def validate_panel(adapter: WriteSmokeAdapter):
    status = await adapter.get_server_status()
    if status.panel_version.removeprefix("v") != "3.8.5" or status.xray_state != "running":
        raise SmokeError("Panel version/running status mismatch")
    if status.xray_version.removeprefix("v") != "26.9.30":
        raise SmokeError("Xray version mismatch")
    inbound = await adapter.get_inbound(6)
    if (inbound.protocol != "vless" or (inbound.stream_settings or {}).get("security") != "reality"
            or (inbound.stream_settings or {}).get("network") != "xhttp"
            or VPNService._validate_provisioning_capability(inbound, "") != ""):
        raise SmokeError("Production trial requires supported VLESS Reality XHTTP")
    return {"panel": "3.8.5", "xray": "26.9.30", "inbound": 6,
            "enabled": True, "protocol": "vless", "security": "reality", "network": "xhttp"}


async def require_absent(adapter, state):
    try:
        await adapter.get_client(state.email)
    except XUINotFoundError:
        return
    raise SmokeError("Test email already exists; never reuse or mutate it")


def verify_client(client, state, expiry):
    assert_identity(client, state)
    if (client.expiry_time_ms != expiry or not client.enable or client.flow != ""
            or client.limit_ip != 1 or client.limit_hwid != 0 or client.total_bytes != 0):
        raise SmokeError("Canonical smoke state differs from intended state")


def verify_links(links, state):
    matched = 0
    for link in links:
        parsed = urlsplit(link)
        if parsed.scheme != "vless":
            continue
        query = parse_qs(parsed.query, keep_blank_values=True)
        if (parsed.username != state.uuid or query.get("type") != ["xhttp"]
                or query.get("security") != ["reality"]
                or query.get("flow", [""]) != [""]):
            raise SmokeError("Subscription VLESS identity/transport mismatch")
        matched += 1
    if not matched:
        raise SmokeError("Subscription has no matching VLESS XHTTP configuration")
    return matched


async def fetch_subscription(adapter, uri, state):
    """Explicit optional GET only. No Bearer/cookies/proxies/redirects/body logging."""
    url = XUIAdapter.validate_subscription_base_url(uri) + quote(state.sub_id, safe="")
    adapter.subscription_requests += 1
    async with aiohttp.ClientSession(timeout=adapter._timeout,
            cookie_jar=aiohttp.DummyCookieJar(), trust_env=False) as session:
        with patch.object(aiohttp.client, "IDEMPOTENT_METHODS", frozenset()):
            async with session.get(url, allow_redirects=False) as response:
                if response.status != 200:
                    raise SmokeError("HTTP subscription response failed")
                body = bytearray()
                async for chunk in response.content.iter_chunked(16384):
                    body.extend(chunk)
                    if len(body) > 1024 * 1024:
                        raise SmokeError("HTTP subscription body oversized")
                if not body:
                    raise SmokeError("HTTP subscription body empty/oversized")
    text = body.decode("utf-8")
    if "vless://" not in text:
        try:
            text = base64.b64decode("".join(text.split()), validate=True).decode("utf-8")
        except (ValueError, UnicodeError):
            raise SmokeError("Unsupported subscription response format") from None
    return verify_links(text.splitlines(), state)


async def run_stage(config: SmokeConfig, state: SmokeState, stage: str,
                    persist: Callable[[SmokeState], None], *, fetch_http=False,
                    adapter_factory=WriteSmokeAdapter) -> dict:
    state.validate(config)
    allowed_states = {
        "preflight": {"PREPARED", "PREFLIGHT_PASS"}, "create": {"PREFLIGHT_PASS"},
        "verify-create": {"CREATE_PASS", "CREATE_REVIEW", "CREATE_ATTEMPTED", "VERIFY_CREATE_PASS"},
        "update": {"VERIFY_CREATE_PASS"},
        "verify-update": {"UPDATE_PASS", "UPDATE_REVIEW", "UPDATE_ATTEMPTED", "VERIFY_UPDATE_PASS"},
        "cleanup": STATES - {"PREPARED", "PREFLIGHT_PASS", "CLEANUP_ATTEMPTED", "CLEANUP_REVIEW",
                              "CLEANUP_PERSISTED", "VERIFY_CLEANUP_PASS"},
        "verify-cleanup": {"CLEANUP_ATTEMPTED", "CLEANUP_REVIEW", "CLEANUP_PERSISTED", "VERIFY_CLEANUP_PASS"},
    }
    if stage not in allowed_states or state.stage not in allowed_states[stage]:
        raise SmokeError("Stage journal blocks this operation; no automatic retry")
    if fetch_http and stage not in ("verify-create", "verify-update"):
        raise SmokeError("Subscription GET only available in verification stage")
    summary = {"stage": stage, "outcome": "FAIL"}
    async with adapter_factory(config, state, stage) as adapter:
        try:
            summary["preflight"] = await validate_panel(adapter)
            if stage in ("preflight", "create"):
                await require_absent(adapter, state)
                if state.initial_expiry <= int(datetime.now(timezone.utc).timestamp() * 1000) + 60000:
                    raise SmokeError("Prepared expiry is stale; do not recreate identity automatically")
                if state.initial_expiry > int(datetime.now(timezone.utc).timestamp() * 1000) + DAY_MS + 60000:
                    raise SmokeError("Prepared expiry exceeds the one-day trial limit")
                if stage == "preflight":
                    persist(replace(state, stage="PREFLIGHT_PASS"))
                else:
                    desired = state.create_request()
                    state = replace(state, stage="CREATE_ATTEMPTED")
                    persist(state)  # A crash after this point cannot replay create.
                    adapter.arm_mutation({"client": desired.client_payload(), "inboundIds": [6]})
                    result = await adapter.add_client(desired)
                    confirmed = result.node_pending is False and adapter.mutation_count == 1
                    state = replace(state, stage="CREATE_PASS" if confirmed else "CREATE_REVIEW",
                                    preservation_digest=preservation_digest(result.client))
                    persist(state)
                    summary["node_pending"] = result.node_pending
                    if not confirmed:
                        raise SmokeError("Persistence confirmed; activation requires manual review")
            elif stage in ("verify-create", "verify-update"):
                client = await adapter.get_client(state.email)
                verify_client(client, state, state.initial_expiry if stage == "verify-create" else state.updated_expiry)
                digest = preservation_digest(client)
                if state.preservation_digest and digest != state.preservation_digest:
                    raise SmokeError("Preserved client fields changed")
                traffic = await adapter.get_client_traffic(state.email)
                if traffic is None:
                    raise SmokeError("Traffic record missing")
                uri = await adapter.get_subscription_base_url()
                links = await adapter.get_subscription_links(state.sub_id)
                summary["share_links_verified"] = verify_links(links, state)
                summary["traffic_read"] = True  # traffic.id is never credential identity.
                parsed = urlsplit(uri)
                summary["subscription_base"] = {"scheme": parsed.scheme, "hostname": parsed.hostname,
                                                "path": parsed.path}
                if fetch_http:
                    summary["http_subscription_verified"] = await fetch_subscription(adapter, uri, state)
                else:
                    summary["http_subscription_verified"] = "NOT RUN (explicit --fetch-subscription required)"
                # Read persistence alone cannot promote an uncertain mutation.
                if state.stage not in ("CREATE_PASS", "VERIFY_CREATE_PASS", "UPDATE_PASS", "VERIFY_UPDATE_PASS"):
                    raise SmokeError("Persistence verified; activation unknown, manual review required")
                persist(replace(state, stage="VERIFY_CREATE_PASS" if stage == "verify-create" else "VERIFY_UPDATE_PASS",
                                preservation_digest=digest))
            elif stage == "update":
                current = await adapter.get_client(state.email)
                verify_client(current, state, state.initial_expiry)
                if not state.preservation_digest or preservation_digest(current) != state.preservation_digest:
                    raise SmokeError("Update preservation snapshot changed")
                # Only one membership is permitted, and it was freshly validated above.
                desired = XUIClientWrite.from_client(current, expiry_time_ms=state.updated_expiry,
                                                     limit_ip=current.limit_ip, enable=current.enable)
                state = replace(state, stage="UPDATE_ATTEMPTED")
                persist(state)
                adapter.arm_mutation(desired.client_payload())
                result = await adapter.update_client(current, desired)
                confirmed = result.node_pending is False and adapter.mutation_count == 1
                persist(replace(state, stage="UPDATE_PASS" if confirmed else "UPDATE_REVIEW"))
                summary["node_pending"] = result.node_pending
                if not confirmed:
                    raise SmokeError("Update activation requires manual review")
            elif stage == "cleanup":
                current = await adapter.get_client(state.email)
                assert_identity(current, state)
                state = replace(state, stage="CLEANUP_ATTEMPTED")
                persist(state)
                summary["cleanup"] = await adapter.cleanup_test_client()
                persist(replace(state, stage="CLEANUP_PERSISTED"))
            else:  # verify-cleanup
                await require_absent(adapter, state)
                persist(replace(state, stage="VERIFY_CLEANUP_PASS"))
                summary["activation"] = "UNKNOWN; canonical absence is not node verification"
            summary["outcome"] = "PASS"
        except Exception as error:
            # No exception payload/chains, remote messages, identifiers or URLs.
            summary["error_type"] = type(error).__name__
            if isinstance(error, SmokeError):
                summary["reason"] = str(error)  # Our static, safe guard diagnostics only.
            if state.stage in ("CREATE_ATTEMPTED", "UPDATE_ATTEMPTED", "CLEANUP_ATTEMPTED"):
                try:
                    persist(replace(state, stage=state.stage.replace("ATTEMPTED", "REVIEW")))
                except OSError:
                    # Durable ATTEMPTED already blocks replay if HTTP was sent.
                    # Preserve counters even when local disk recovery also fails.
                    summary["journal"] = "WRITE FAILED; manual review required"
        finally:
            summary["panel_requests"] = len(adapter.requests)
            summary["mutations"] = adapter.mutation_count
            summary["subscription_requests"] = adapter.subscription_requests
            if adapter.mutation_response_metadata is not None:
                summary["mutation_response_metadata"] = asdict(adapter.mutation_response_metadata)
    # A hostile server could echo a credential as subURI hostname/path.
    text = json.dumps(summary)
    for secret in (config.token, state.uuid, state.sub_id):
        text = text.replace(secret, "<redacted>")
    return json.loads(text)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    for stage in STAGES:
        group.add_argument("--" + stage, dest="stage", action="store_const", const=stage)
    parser.add_argument("--confirm-stage", choices=STAGES[1:], help="Explicit approval for this stage only")
    parser.add_argument("--fetch-subscription", action="store_true", help="Extra HTTP GET; requires separate approval")
    args = parser.parse_args(argv)
    logging.disable(logging.CRITICAL)
    try:
        config = SmokeConfig.from_env(load_local_env())
        if config.inbound_id != 6:
            raise SmokeError("This smoke is restricted to inbound 6")
        if args.stage != "dry-run" and args.confirm_stage != args.stage:
            raise SmokeError("Network stage requires matching explicit --confirm-stage")
        for path in (STATE_PATH, LOCK_PATH, OUTCOME_PATH):
            ignored = subprocess.run(["git", "check-ignore", "--quiet", "--", path.name], cwd=ROOT)
            tracked = subprocess.run(["git", "ls-files", "--error-unmatch", "--", path.name],
                                     cwd=ROOT, capture_output=True)
            if ignored.returncode != 0 or tracked.returncode == 0:
                raise SmokeError("Local smoke files must be gitignored/untracked")
        with exclusive_run(LOCK_PATH):
            if args.stage == "dry-run" and not STATE_PATH.exists():
                save_state(STATE_PATH, SmokeState.generate(config))
            state = load_state(STATE_PATH, config)
            planned = planned_summary(config, state)
            print(json.dumps(planned, indent=2), flush=True)
            if args.stage == "dry-run":
                return 0
            summary = asyncio.run(run_stage(config, state, args.stage,
                lambda updated: save_state(STATE_PATH, updated), fetch_http=args.fetch_subscription))
            if "mutation_response_metadata" in summary:
                # Separate safe diagnostic artifact; never rewrite historical
                # journal outcomes or retain request/response bodies.
                OUTCOME_PATH.write_text(json.dumps({"stage": args.stage,
                    "mutation_response_metadata": summary["mutation_response_metadata"]}, indent=2),
                    encoding="utf-8")
            print(json.dumps(summary, indent=2))
            return 0 if summary["outcome"] == "PASS" else 1
    except (SmokeError, XUIError, ValueError, OSError, TypeError) as error:
        print(json.dumps({"outcome": "FAIL", "error_type": type(error).__name__,
                          "diagnostic": "Stage/config/journal rejected; no fallback or retry"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
