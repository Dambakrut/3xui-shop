# Patch 6C — Production read-only smoke

## Controlled live result: reads PASS, shop compatibility FAIL

- Checked at UTC: 2026-10-02 19:10:30.
- Exactly one controlled live sequence executed after explicit authorization.
- Production HTTP requests: **2**, both successful GET responses parsed by adapter.
- Bearer authentication: **PASS**. No session login, CSRF bootstrap or cookies used.
- Panel version: **3.8.5**; Xray version: **26.9.30**; state: **running**.
- Inbound **6**: found, enabled **true**, protocol **vless**.
- Security: **reality**. Network: **xhttp**. disableFlow: **false**.
- Remark: Germany. Tag: **in-38942-tcp**. Actual stream network is xhttp despite tag suffix.
- Client, traffic and subscription requests: **0** (not authorized/configured).
- Retries/fallback authentication: **0**.
- Client writes, inbound mutations, setting mutations: **0**.

The only production requests were:

1. GET /panel/api/server/status (under the explicitly approved panel base path)
2. GET /panel/api/inbounds/list (same base path)

Safe diagnostic: `SmokeError` at inbound stage:
`Shop Vision assumptions are not confirmed; stopped`.

The HTTP/read contracts passed. The overall smoke returned FAIL because current
shop create policy requires VLESS TCP with TLS/Reality and Vision; observed network
is xhttp. This does not establish that panel/Xray XHTTP is broken or that XHTTP
never supports Vision. No encryption/decryption assumptions were investigated via
additional requests, no runtime compatibility guard was relaxed, and production
was not changed. Inbound tag is not used to infer network compatibility.

**Readiness for 6D: NOT READY.** Inbound 6 is outside the current shop creation
policy. Any protocol-policy expansion or a separate test inbound requires a later
explicit task. No SHOP-TEST inbound/client was created and no write was attempted.

## Prior dry-run verification

- Checked at UTC: 2026-10-02 19:04:17.
- Branch: feature/3xui-385-live-read-smoke.
- Base commit: c668358 (completed modern write integration and node_pending fix).
- Expected panel: 3x-ui 3.8.5. Expected Xray Core: 26.9.30.
- Auth mode configured: token. Required scope: admin.
- Configured inbound ID: 6.
- Optional client target: not configured.
- Optional subscription target: not configured.
- Local .env: gitignored and untracked; no secret copied into this document.
- At the dry-run stage, working tree contained user changes to .env.example and
  four Patch 6B.2b review exports. The user restored/removed them before live run.
  Before live execution only the three intended Patch 6C files were untracked;
  there were no unrelated tracked changes.

| Check | Result |
|---|---|
| Config parsing | PASS |
| Dry-run plan | PASS |
| Network requests in dry-run | 0 |
| Live Bearer auth | PASS |
| Live panel/Xray version | PASS: 3.8.5 / 26.9.30, running |
| Live inbound read | PASS: ID 6 exists and enabled |
| Shop creation compatibility | FAIL: xhttp, policy requires tcp |
| Canonical client / membership | SKIPPED: no client target |
| Traffic | SKIPPED: no client target |
| Subscription settings | SKIPPED: no subscription target |
| Readiness for 6D | NOT READY: protocol-policy mismatch |

The initial dry-run performed zero network requests. Observed production results
are recorded separately above; no raw production response was saved.

## Planned HTTP sequence

Paths below are relative to the configured panel web base path. Dry-run prints
the exact full paths locally without host/token. No admin base path is saved here.

1. GET /panel/api/server/status
2. GET /panel/api/inbounds/list

get_inbound(6) fetches that list once and filters by exact ID. A failure stops the
sequence. No polling/retries/session-login fallback. Client and traffic reads
are included only when XUI_SMOKE_CLIENT_EMAIL is configured. Subscription settings
read is included only when XUI_SMOKE_SUB_ID is configured. subLinks is not called.

## Guard and authentication

scripts/xui_live_readonly_smoke.py uses a dedicated token-only adapter subclass.
The exact per-run method/path allowlist is checked before HTTP/session creation.
Unconfigured targets, unknown GETs, unknown POSTs, login/CSRF, client mutations,
inbound/settings/node mutations and legacy endpoints are blocked. The actual live
config/plan was asserted to contain exactly the two authorized GETs, inbound ID 6,
the approved base path, and no optional client/subscription targets. The public
add_client/update_client methods also fail immediately. Cookie storage is disabled;
Bearer remains the only authentication mechanism. TLS verification remains ON,
redirects are disabled, and no request is retried. Locked aiohttp 3.10.11 normally
retries GET once on a disconnected response. This standalone sequential smoke
temporarily disables its IDEMPOTENT_METHODS retry classification around each
request and restores it afterward. A loopback connection-drop test proves one
HTTP attempt, not two. Application runtime and dependency versions were unchanged.

Potential allowlist:

- GET /panel/api/server/status
- GET /panel/api/inbounds/list
- GET /panel/api/clients/get/{configured-email}
- GET /panel/api/clients/traffic/{configured-email}
- POST /panel/api/setting/all with an empty body, only for subscription section

The official v3.8.5 [scope middleware](https://github.com/MHSanaei/3x-ui/blob/v3.8.5/internal/web/controller/api.go)
permits status for monitor/node-sync/admin; inbound list for node-sync/admin;
canonical client, traffic and settings reads require admin scope.
The [settings controller](https://github.com/MHSanaei/3x-ui/blob/v3.8.5/internal/web/controller/setting.go)
maps POST /all to getAllSetting, which reads GetAllSettingView and returns JSON.
Setting updates use a separate /update handler. Thus the allowlisted POST is a
read, not a setting mutation. Permission failures are never bypassed.

## Manual usage

Configure XUI_HOST, explicit XUI_AUTH_MODE=token, XUI_API_TOKEN and XUI_INBOUND_ID
in process env or the gitignored root .env. Optional XUI_SMOKE_CLIENT_EMAIL should
be your own/test client. Optional XUI_SMOKE_SUB_ID enables subscription settings
verification; the ID is never printed or put in an endpoint. No secret CLI arguments.

```powershell
.\.venv-runtime-test\Scripts\python.exe scripts/xui_live_readonly_smoke.py --dry-run
```

Running without --dry-run performs one bounded read sequence. One restricted live
run has now been executed; no additional run is authorized by that execution.
Review a fresh exact plan and obtain separate authorization before any later run.
A missing/invalid subURI is a blocker; no manual URL fallback
or production setting changes are attempted by this script.

The script prints only a curated summary; it never persists production payloads.
DEBUG logging is disabled for the command. Token, full UUID/subId, full subscription
URL, cookies, CSRF, personal email and share links are excluded/redacted. Later
live results may be recorded here only as the allowed summary fields.

CLIENT WRITE REQUESTS: 0

INBOUND MUTATIONS: 0

SETTING MUTATIONS: 0

No Patch 6D work, payment/Telegram E2E or production changes were performed.

## Final local regression

- Locked Python 3.12: **169 tests passed, 0 skips** (12 smoke/guard tests).
- poetry check --lock: PASS; existing Poetry metadata deprecation warnings only.
- pip check: PASS (no broken requirements).
- git diff --check: PASS; new file whitespace check also PASS.
- No commit/push. Only three intended new Patch 6C files are untracked.
