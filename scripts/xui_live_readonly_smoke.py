"""Manual, guarded 3x-ui 3.8.5 smoke. Never imported by application startup."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
import os
import re
from pathlib import Path
import subprocess
import sys
from typing import Mapping
from unittest.mock import patch
from urllib.parse import quote, urlsplit

import aiohttp

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import dotenv_values

from app.integrations.xui import XUIAdapter, XUIAuthMode, XUIError, is_member_of


class SmokeError(RuntimeError):
    """Only static, safe diagnostic messages are used."""


@dataclass(frozen=True)
class SmokeConfig:
    host: str = field(repr=False)
    token: str = field(repr=False)
    inbound_id: int
    client_email: str | None = field(default=None, repr=False)
    sub_id: str | None = field(default=None, repr=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> SmokeConfig:
        if env.get("XUI_AUTH_MODE") != "token":
            raise SmokeError("XUI_AUTH_MODE must explicitly be token")
        host, token = env.get("XUI_HOST", ""), env.get("XUI_API_TOKEN", "")
        try:
            url = urlsplit(host)
            url.port
        except ValueError:
            raise SmokeError("Invalid XUI_HOST") from None
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or url.query or url.fragment or "\\" in host
                or "%" in url.path
                or any(c.isspace() or ord(c) < 32 for c in host)
                or any(p in (".", "..") for p in url.path.split("/"))):
            raise SmokeError("XUI_HOST must be an HTTPS panel URL without credentials/query")
        if not token or any(c.isspace() or ord(c) < 32 for c in token):
            raise SmokeError("XUI_API_TOKEN is required and must not contain whitespace")
        try:
            inbound_id = int(env.get("XUI_INBOUND_ID", ""))
        except ValueError:
            raise SmokeError("XUI_INBOUND_ID must be a positive integer") from None
        if inbound_id <= 0:
            raise SmokeError("XUI_INBOUND_ID must be a positive integer")
        email = env.get("XUI_SMOKE_CLIENT_EMAIL") or None
        sub_id = env.get("XUI_SMOKE_SUB_ID") or None
        for value in (email, sub_id):
            if value and (value in (".", "..") or any(
                    c.isspace() or ord(c) < 32 or c in "/\\%?#" for c in value)):
                raise SmokeError("Invalid optional smoke target")
        return cls(host, token, inbound_id, email, sub_id)

    def request_plan(self) -> tuple[tuple[str, str], ...]:
        plan = [("GET", "panel/api/server/status"), ("GET", "panel/api/inbounds/list")]
        if self.client_email:
            email = quote(self.client_email, safe="")
            plan.extend((("GET", f"panel/api/clients/get/{email}"),
                         ("GET", f"panel/api/clients/traffic/{email}")))
        if self.sub_id:
            # This is an upstream read handler despite its POST method.
            plan.append(("POST", "panel/api/setting/all"))
        return tuple(plan)


class ReadOnlySmokeAdapter(XUIAdapter):
    """Per-target exact method/path allowlist before any HTTP session is used."""

    def __init__(self, config: SmokeConfig):
        self._allowed_requests = frozenset(config.request_plan())
        self.requests_attempted = 0
        super().__init__(config.host, auth_mode=XUIAuthMode.TOKEN,
                         token=config.token, timeout_seconds=10)

    async def _request(self, method, endpoint, **kwargs):
        if (method, endpoint) not in self._allowed_requests:
            raise SmokeError("HTTP request blocked by read-only smoke allowlist")
        if method == "POST" and (endpoint != "panel/api/setting/all"
                                  or kwargs.get("json_body") not in (None, {})):
            raise SmokeError("POST blocked by read-only smoke allowlist")
        self.requests_attempted += 1
        # Locked aiohttp 3.10.11 otherwise retries idempotent GET once after
        # ClientOSError/ServerDisconnectedError, even without our own retry loop.
        # This standalone script sends requests sequentially. Scope the override
        # to that one await and restore it afterwards; wire method stays GET.
        if not hasattr(aiohttp.client, "IDEMPOTENT_METHODS"):
            raise SmokeError("Unsupported aiohttp retry policy; request blocked")
        with patch.object(aiohttp.client, "IDEMPOTENT_METHODS", frozenset()):
            return await super()._request(method, endpoint, **kwargs)

    def _get_session(self):
        if self._closed:
            raise SmokeError("Smoke adapter is closed")
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=self._timeout, cookie_jar=aiohttp.DummyCookieJar(), trust_env=False,
            )
        return self._session

    async def add_client(self, *_args, **_kwargs):
        raise SmokeError("Client mutation blocked by read-only smoke guard")

    async def update_client(self, *_args, **_kwargs):
        raise SmokeError("Client mutation blocked by read-only smoke guard")


def redact(value: str) -> str:
    return f"{value[:8]}…{value[-4:]}" if len(value) > 12 else "<redacted>"


def safe_summary(value, secrets: tuple[str, ...]):
    """Scrub configured secrets even if a remote display field echoes one."""
    if isinstance(value, dict):
        return {key: safe_summary(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [safe_summary(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "<redacted>")
        value = re.sub(r"[a-zA-Z][a-zA-Z0-9+.-]*://\S+", "<redacted-url>", value)
    return value


def planned_summary(config: SmokeConfig) -> dict:
    prefix = urlsplit(config.host).path.rstrip("/")
    # Optional client paths are displayed with a placeholder, not a personal email.
    paths = [{"method": method, "path": prefix + "/" + (
        endpoint.replace(quote(config.client_email, safe=""), "{configured-email}")
        if config.client_email and endpoint.startswith("panel/api/clients/") else endpoint
    )} for method, endpoint in config.request_plan()]
    return {
        "mode": "DRY_RUN_ONLY", "network_requests": 0,
        "auth_mode": "token", "required_token_scope": "admin",
        "expected_panel_version": "3.8.5", "expected_xray_version": "26.9.30",
        "configured_inbound_id": config.inbound_id,
        "client_target_configured": bool(config.client_email),
        "subscription_target_configured": bool(config.sub_id),
        "planned_requests": paths,
        "client_write_requests": 0, "inbound_mutations": 0, "setting_mutations": 0,
    }


async def run_smoke(config: SmokeConfig, *, dry_run: bool,
                    expected_panel_version: str = "3.8.5") -> dict:
    if expected_panel_version not in ("3.8.5", "3.9.0"):
        raise SmokeError("Unsupported expected panel version")
    summary = planned_summary(config)
    summary["expected_panel_version"] = expected_panel_version
    if dry_run:
        return safe_summary(summary, (config.token, config.sub_id or ""))
    summary.update(mode="LIVE_READ_ONLY", outcome="FAIL")
    stage = "server_status"
    secrets = (config.token, config.sub_id or "")
    async with ReadOnlySmokeAdapter(config) as adapter:
        try:
            status = await adapter.get_server_status()
            summary["server_status"] = {
                "panel_version": status.panel_version, "xray_version": status.xray_version,
                "xray_state": status.xray_state,
            }
            if status.panel_version.removeprefix("v") != expected_panel_version:
                raise SmokeError("Panel version mismatch; stopped")
            if status.xray_version != "26.9.30":
                raise SmokeError("Xray version mismatch; stopped")
            if status.xray_state != "running":
                raise SmokeError("Xray is not running; stopped")
            stage = "inbound"
            # get_inbound performs exactly one list request and an exact local ID lookup.
            inbound = await adapter.get_inbound(config.inbound_id)
            settings = inbound.stream_settings or {}
            if any(not isinstance(settings.get(key), str) for key in ("security", "network")):
                raise SmokeError("Inbound security/network fields missing or invalid; stopped")
            summary["inbound"] = {
                "id": inbound.id, "enabled": inbound.enable, "protocol": inbound.protocol,
                "remark": inbound.remark[:80], "tag": inbound.tag[:80],
                "security": settings.get("security"), "network": settings.get("network"),
                "disableFlow": inbound.raw.get("disableFlow"),
            }
            if not inbound.enable:
                raise SmokeError("Configured inbound is disabled; stopped")
            compatible = (inbound.protocol == "vless" and settings.get("network") == "tcp"
                          and settings.get("security") in ("tls", "reality")
                          and inbound.raw.get("disableFlow") is False)
            summary["inbound"]["vision_compatible"] = compatible
            if expected_panel_version == "3.8.5" and not compatible:
                raise SmokeError("Shop Vision assumptions are not confirmed; stopped")
            if expected_panel_version == "3.9.0":
                from app.bot.services.vpn import VPNService, VPNReadError
                try:
                    summary["inbound"]["resolved_flow"] = VPNService._validate_provisioning_capability(inbound, None)
                except VPNReadError:
                    raise SmokeError("Shop capability policy rejected inbound; stopped") from None
            if expected_panel_version == "3.9.0":
                excluded = inbound.raw.get("excludeFromSub")
                if type(excluded) is not bool:
                    raise SmokeError("Subscription visibility field missing or invalid; stopped")
                summary["inbound"]["excludeFromSub"] = excluded
                if excluded:
                    raise SmokeError("Configured inbound is excluded from subscriptions; stopped")
            if config.client_email:
                stage = "client"
                client = await adapter.get_client(config.client_email)
                secrets += (client.uuid, client.sub_id, client.email)
                member = is_member_of(client, config.inbound_id)
                summary["client"] = {
                    "found": True, "uuid_redacted": redact(client.uuid),
                    "sub_id_redacted": redact(client.sub_id), "enabled": client.enable,
                    "expiry_time_ms": client.expiry_time_ms, "total_bytes": client.total_bytes,
                    "limit_ip": client.limit_ip, "limit_hwid": client.limit_hwid,
                    "sub_id_present": bool(client.sub_id), "flow": client.flow,
                    "inbound_ids": list(client.inbound_ids), "configured_membership": member,
                    "shared_client": len(client.inbound_ids) > 1,
                }
                if not member:
                    raise SmokeError("Configured client membership mismatch; stopped")
                stage = "traffic"
                traffic = await adapter.get_client_traffic(config.client_email)
                if traffic is None:
                    raise SmokeError("Client traffic row missing; stopped")
                summary["traffic"] = {
                    "read": True, "up": traffic.up, "down": traffic.down, "total": traffic.total,
                    "expiry_time_ms": traffic.expiry_time_ms, "enabled": traffic.enable,
                    "numeric_id_used_as_uuid": False,
                }
            if config.sub_id:
                stage = "subscription_config"
                uri = urlsplit(await adapter.get_subscription_base_url())
                summary["subscription_config"] = {
                    "scheme": uri.scheme, "hostname": uri.hostname, "path": uri.path,
                }
            summary["outcome"] = "PASS"
        except (XUIError, SmokeError) as error:
            # Never print exception text/chains from third-party transport or payloads.
            summary["failure"] = {"stage": stage, "error_type": type(error).__name__}
            if isinstance(error, SmokeError):
                summary["failure"]["reason"] = str(error)
        finally:
            summary["network_requests"] = adapter.requests_attempted
    return safe_summary(summary, secrets)


def load_local_env() -> dict[str, str]:
    path = ROOT / ".env"
    values = {}
    if path.is_file():
        ignored = subprocess.run(["git", "check-ignore", "--quiet", "--", ".env"],
                                 cwd=ROOT, capture_output=True)
        tracked = subprocess.run(["git", "ls-files", "--error-unmatch", ".env"],
                                 cwd=ROOT, capture_output=True)
        if ignored.returncode != 0 or tracked.returncode == 0:
            raise SmokeError("Local .env must be gitignored and untracked")
        values = {key: value for key, value in dotenv_values(path).items() if value is not None}
    return {**values, **os.environ}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Validate and plan; no HTTP session")
    parser.add_argument("--panel-version", choices=("3.8.5", "3.9.0"), default="3.8.5",
                        help="Explicit expected version; no automatic version fallback")
    args = parser.parse_args(argv)
    # Prevent inherited DEBUG configuration exposing HTTP/personal data.
    logging.disable(logging.CRITICAL)
    try:
        config = SmokeConfig.from_env(load_local_env())
        summary = asyncio.run(run_smoke(config, dry_run=args.dry_run,
                                      expected_panel_version=args.panel_version))
    except (SmokeError, XUIError, ValueError, OSError) as error:
        print(json.dumps({"outcome": "FAIL", "error_type": type(error).__name__,
                          "reason": "Smoke config/runtime validation failed; no fallback"}))
        return 1
    summary["checked_at_utc"] = datetime.now(timezone.utc).isoformat()
    print(json.dumps(summary, ensure_ascii=True, indent=2))
    return 0 if args.dry_run or summary.get("outcome") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
