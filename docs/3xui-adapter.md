# 3x-ui 3.8.5 adapter foundation (Patch 6B.1)

The `app.integrations.xui` package is a **read-only**, asynchronous HTTP client for the [MHSanaei/3x-ui v3.8.5](https://github.com/MHSanaei/3x-ui/releases/tag/v3.8.5) API. Production panel version is reported as 3.8.5; Xray Core version is reported as 26.9.30. These tests use local fakes, never the production panel. Shop read paths use this adapter as of Patch 6B.2a; write paths remain legacy py3xui 0.3.2.

## Authentication

`XUIAuthMode.SESSION` uses one `aiohttp.ClientSession` with its own cookie jar per adapter instance. It GETs `<base>/csrf-token`, validates `{success:true,obj:<nonempty string>}`, retains the returned `3x-ui` cookie and token, then POSTs `<base>/login` with JSON `username`, `password`, optional `twoFactorCode`, and `X-CSRF-Token`. The login response must report success and establish the session cookie. The upstream login saves the user in that same session; the checked [CSRF source](https://github.com/MHSanaei/3x-ui/blob/v3.8.5/internal/web/session/csrf.go) does not rotate the CSRF token on login. The retained `csrf_token` property is for a later write-capable patch; no write API is exposed now.

`XUIAuthMode.TOKEN` sends `Authorization: Bearer <token>` on read calls and never calls login. `authenticate()` only prepares this mode locally; token validity is determined by the first API response. [Backend scope allowlists](https://github.com/MHSanaei/3x-ui/blob/v3.8.5/internal/web/controller/api.go) allow `monitor` to read `/server/status` but not inbound/client data. `node-sync` permits inbound list and some writes, but not all canonical client, traffic and link reads. An **admin-scope** token is therefore required for the adapter's complete read surface. No token creation or scope escalation is implemented.

The `XUI_TOKEN` setting of the existing shop has legacy py3xui loginSecret semantics. It is not interpreted as a Bearer token. Runtime config uses `XUI_AUTH_MODE=session` by default and requires `XUI_USERNAME`/`XUI_PASSWORD`. Explicit `XUI_AUTH_MODE=token` requires the separate `XUI_API_TOKEN` and does not require username/password. The runtime passes these credentials only to the matching adapter mode. A static two-factor code is not configured by the shop; a 2FA-enabled session account needs a separate approved strategy, or an appropriate API token.

## Read methods

All paths include the configured panel web base path. The method signatures live in `adapter.py`.

| Method | HTTP route | Result |
|---|---|---|
| `authenticate()` | GET `/csrf-token`, POST `/login` in session mode; no HTTP in token mode | Authenticated cookie+CSRF or configured Bearer mode |
| `get_server_status()` | GET `/panel/api/server/status` | `XUIServerStatus(panel_version, xray_version, xray_state)` |
| `list_inbounds()` | GET `/panel/api/inbounds/list` | Tuple of `XUIInboundSummary`, preserving all returned IDs and order |
| `get_inbound(id)` | Filters `list_inbounds()` | Exact ID or `XUINotFoundError` |
| `get_client(email)` | GET `/panel/api/clients/get/{email}` | `XUIClient` from `obj.client` and **all** `obj.inboundIds` |
| `get_client_traffic(email)` | GET `/panel/api/clients/traffic/{email}` | `XUIClientTraffic` or `None` when `obj:null` |
| `get_subscription_links(sub_id)` | GET `/panel/api/clients/subLinks/{subId}` | Tuple of protocol share URLs |

**Correction to Patch 6A:** [the official endpoint contract](https://github.com/MHSanaei/3x-ui/blob/v3.8.5/docs/public/openapi.json) and [subscription link provider](https://github.com/MHSanaei/3x-ui/blob/v3.8.5/internal/sub/links.go) show that `/clients/subLinks/{subId}` returns protocol URLs such as `vless://`, `vmess://` and `trojan://`. It does **not** return the HTTP(S) subscription endpoint URL that `VPNService.get_key()` builds. The requested adapter method name is retained, but its documented result is exact. Resolving the HTTP subscription URL from panel settings remains a separate 6B.2 task. No URL is constructed manually by this adapter.

## Identity and isolation

`XUIClient.record_id` is the numeric client database key. `XUIClient.uuid` is the canonical credential UUID from `obj.client.uuid`; the adapter never substitutes the traffic row ID. `XUIClientTraffic.id` is a separate numeric traffic record key, with optional `uuid` solely as traffic metadata. `email` is the canonical lookup key. All `inboundIds` are preserved as a tuple; `is_member_of()` and `validate_client_membership()` check a **caller-supplied positive configured ID** without choosing an inbound. No first-inbound selection exists.

Models validate the fields used for identity, quotas, expiry and future update preservation. Their `raw` dictionaries retain other current backend fields without using them for typed identity checks. Raw payloads, URLs, credentials, cookies and CSRF tokens are not logged; raw fields are hidden from model repr. This foundation does not yet establish ownership of an existing 3x-ui client or prevent edits to a multi-inbound client: there are no edit methods.

## Errors, TLS and lifetime

Malformed JSON, malformed envelope, missing required objects and invalid typed fields raise `XUIProtocolError`. `success:false` raises `XUIAPIError`, except the canonical lookup's GORM `record not found` message is mapped to `XUINotFoundError`. Other locale/DB errors remain API errors; callers must not infer absence from them. HTTP 401/403 and login failures map to auth/authorization errors; transport/timeout and other HTTP failures raise `XUITransportError`. Exception text does not include response bodies, full URLs or secrets. An unauthenticated session request may be masked by upstream as HTTP 404; the adapter treats that case as authentication failure, while a token-mode 404 is not-found. A session auth failure clears local auth state for a later explicit call; there is no automatic replay.

`aiohttp` performs TLS certificate verification by default. No `ssl=False`, custom trust bypass, redirect following or automatic retry is used. `timeout_seconds` bounds each request. One session/cookie jar is lazily created per adapter; use `async with XUIAdapter(...)` or `await close()`. A closed adapter cannot be reused. No public create, update, delete, disable or inbound modification method exists.

## Fixtures and next integration step

`tests/fixtures/xui/v3_8_5/` contains minimal synthetic envelopes derived from the checked [controllers](https://github.com/MHSanaei/3x-ui/blob/v3.8.5/internal/web/controller/client.go), [backend models](https://github.com/MHSanaei/3x-ui/blob/v3.8.5/internal/database/model/model.go), [traffic model](https://github.com/MHSanaei/3x-ui/blob/v3.8.5/internal/xray/client_traffic.go), [server status](https://github.com/MHSanaei/3x-ui/blob/v3.8.5/internal/web/service/server.go) and subscription link provider. They are not production captures. `tests/test_xui_adapter.py` drives a local aiohttp HTTP server to verify wire behavior and failure handling.

## Runtime integration status (Patch 6B.2a)

`ServerPoolService` owns one `XUIAdapter` and session per available server. Startup and refresh authenticate, read `/server/status`, require panel v3.8.5 and running Xray, then list inbounds. Provisioning eligibility requires the exact configured `XUI_INBOUND_ID` to exist, be enabled and have a known panel protocol. The typed inbound summary retains protocol, remark, tag and stream settings for the next patch. Failed candidates are closed immediately; refresh/removal closes old sessions; shutdown and startup failure paths close all remaining adapters. Modern authentication does not invoke py3xui login.

`VPNService.is_client_exists`, `get_client_data` and `get_limit_ip` use canonical client lookup and membership from the adapter. The shop compares `User.vpn_id` only to canonical `client.uuid`; the numeric traffic row ID is never used for identity. Usage and expiry presentation come from the separate traffic response. Missing canonical client is reported as absent. Panel failures, mismatched UUID or missing configured membership are translated to a generic `VPNReadError` for purchase flows; the user-facing status read returns `None`, as before. A failed read must not be interpreted as a missing client by purchase logic.

**Read path: modern 3x-ui 3.8.5 adapter. Write path: legacy py3xui, not modernized.** `create_client` and `update_client` retain their legacy py3xui add/update calls. They do not gain modern write support in 6B.2a. Their preconditions now use modern inbound/canonical checks where safely separable, but `update_client` still performs a legacy lookup solely to hydrate the old write model. The shop does not log py3xui in at startup: its 0.3.2 login is incompatible with v3.8.5 CSRF. On the target panel, legacy add/update therefore fail closed and cannot be considered operational provisioning. Token mode has no legacy write client at all. Do not sell or renew on this transitional branch until 6B.2b supplies modern writes and separately verifies them.

6B.2b must implement modern add/update with exact inbound and ownership policy, preserve credentials and unrelated quota/reset/metadata fields, adapt renewal to canonical/traffic records, handle ambiguous side effects, and resolve the actual HTTP subscription URL from verified panel settings. No production write test is part of 6B.2a.
