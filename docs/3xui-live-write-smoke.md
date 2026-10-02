# 3x-ui 3.8.5 controlled write smoke — create persisted, activation needs review

Target: panel 3.8.5, Xray 26.9.30, Bearer admin token, inbound 6,
VLESS + Reality + XHTTP, empty flow. No Telegram/payment runtime is started.

## Recorded production stages

| Stage | Result |
|---|---|
| PREPARED / dry-run | PASS, 2026-10-02; network requests = 0 |
| PREFLIGHT | PASS, 2026-10-02; exactly 3 authorized GET requests |
| CREATE | REVIEW — canonical persistence confirmed; nodePending unknown |
| VERIFY CREATE | PASS — GET-only persistence/traffic/share-link verification; activation still UNKNOWN |
| UPDATE | NOT RUN |
| VERIFY UPDATE | NOT RUN |
| CLEANUP | NOT RUN |
| VERIFY CLEANUP | NOT RUN |
| HTTP subscription fetch / VPN handshake | NOT RUN |

PRODUCTION READS: 13. PRODUCTION WRITES: 1 (cumulative preflight + create + investigation).

Approved preflight confirmed Bearer authentication, panel 3.8.5, Xray 26.9.30
running, enabled inbound 6 with VLESS/Reality/XHTTP and passing shop capability
policy. Exact synthetic test email was NOT FOUND. Journal is PREFLIGHT_PASS.
The standalone preflight performed no POST, retry or fallback auth.

Separately approved CREATE ran once: 6 panel requests (5 GET + 1 POST /clients/add).
Canonical reconciliation GET confirmed the exact intended test identity and state:
membership [6], enable=true, flow="", limitIp=1, limitHwid=0, quota=0 and planned expiry.
Mutation response did not provide a confirmed nodePending boolean to the smoke;
the current instrumentation does not distinguish a lost/error response from a
successful response with no usable outcome object. Persistence is confirmed,
activation UNKNOWN. Journal is CREATE_REVIEW (manual review required).
Retries=0, redirects=0, fallback=0, login/CSRF=0, subscription requests=0.
Stopped immediately after create reconciliation. No verify-create stage, update,
subscription fetch or cleanup occurred. No further production action is authorized.

## Separately approved GET-only verify investigation

Performed 5 GET requests: server status, inbound list, exact canonical client,
exact client traffic and subLinks for the state-file subId. POST=0, retries=0,
settings/all NOT RUN, HTTP subscription fetch NOT RUN. The stock verification's
settings POST was excluded by a stricter GET-only guard.

Canonical identity matches state UUID/subId (redacted above), memberships exactly
[6], flow="", enable=true, limitIp=1, limitHwid=0, totalGB=0 and expiry exactly the
planned initial value. Preservation hash matches. Traffic read PASS (numeric ID
used only as traffic record identity; up/down/total=0). One VLESS share link passed
UUID/XHTTP/Reality/empty-flow checks; no link or response body was saved.
Panel/core/inbound safety validation also passed. Journal remains CREATE_REVIEW.
UPDATE NOT READY. No additional mutation was performed or authorized.

Create response classification: UNKNOWN. Original HTTP status, response-received
flag, success envelope and obj presence/type were not retained by instrumentation.
The null nodePending result alone cannot distinguish successful null obj from
transport/protocol/API error followed by successful canonical reconciliation.

Important source correction: the original audit did not inspect pendingNodeObj
fully. Official v3.8.5
[pendingNodeObj](https://github.com/MHSanaei/3x-ui/blob/v3.8.5/internal/web/controller/util.go)
returns {nodePending:true} only for pending=true; otherwise nil. A valid success
response without a nodePending field is therefore part of the normal upstream
contract, not automatically a transport fault.
[Create controller](https://github.com/MHSanaei/3x-ui/blob/v3.8.5/internal/web/controller/client.go)
uses that helper. The shop's explicit-false-only acceptance and synthetic tests do
not represent this complete contract. Runtime policy is not changed by this
investigation, and source evidence does not reconstruct the missing real response
or prove activation. No automatic promotion to CREATE_PASS is allowed.

## Prepared plan

Synthetic email: `shop-smoke-20261002194630-cb104e5f`.
UUID/subId: `cb104e5f…e371` (same generated UUID4, matching shop contract).
Membership exactly [6], enable=true, flow="", quota=0 bytes/unlimited,
limitIp=1, limitHwid=0, tgId=0 (no real Telegram user).
Initial expiry: 2026-10-03 19:46:30.600 UTC.
Updated expiry: 2026-10-04 19:46:30.600 UTC.

Preflight allowlist, exactly one request per endpoint:

```text
GET /UKr0zLQHQLTGV91xmI/panel/api/server/status
GET /UKr0zLQHQLTGV91xmI/panel/api/inbounds/list
GET /UKr0zLQHQLTGV91xmI/panel/api/clients/get/shop-smoke-20261002194630-cb104e5f
```

Planned mutations (none executed):

```text
POST /UKr0zLQHQLTGV91xmI/panel/api/clients/add
POST /UKr0zLQHQLTGV91xmI/panel/api/clients/update/shop-smoke-20261002194630-cb104e5f
POST /UKr0zLQHQLTGV91xmI/panel/api/clients/del/shop-smoke-20261002194630-cb104e5f
```

## Operator stages and approval boundary

Run `scripts/xui_live_write_smoke.py` manually in the locked Python 3.12 env.
Required env: XUI_HOST (HTTPS with panel base path), XUI_AUTH_MODE=token,
XUI_API_TOKEN, XUI_INBOUND_ID=6. Local .env must be ignored and untracked.
Optional readonly-smoke targets are not used as write identities.

`--dry-run` generates the local identity/journal only on its first run.
Subsequent runs reuse the same validated identity. All network stages require
`--confirm-stage <matching-stage>` as an explicit operator assertion. This flag
does not replace the user's separate approval for each production stage.

After approval for preflight only:

```powershell
.\.venv-runtime-test\Scripts\python.exe scripts/xui_live_write_smoke.py --preflight --confirm-stage preflight
```

Other separately approved stages: --create, --verify-create, --update,
--verify-update, --cleanup, --verify-cleanup. Each requires its matching confirmation.
One command never chains mutations. Identity and planned mutation paths are
shown redacted before any network operation.

The ignored .xui-write-smoke-state.json stores only synthetic email/UUID/subId,
inbound, intended expiries, stage, panel binding hash and preservation hash.
It contains no token/cookies/CSRF/links/raw payloads. An exclusive ignored lock
serializes CLI runs. Atomic flushed intent is saved before mutation; crashes leave
ATTEMPTED/REVIEW, which cannot re-run the mutation. Missing/corrupt state stops
non-dry stages. A stale lock requires manual journal review, not automatic removal.
Keep the journal after cleanup as local evidence; it is not automatically regenerated.

## HTTP guard and verification

Every stage has an exact method/path allowlist bound to its generated identity.
Only that stage's mutation is permitted; a body guard must be armed after durable
intent and fresh checks. Maximum one mutation attempt per command. No redirects,
proxy-env trust, cookies, login, CSRF bootstrap, TLS bypass or HTTP retries.
The locked aiohttp idempotent transport retry is disabled within each sequential
request; physical POST counts are asserted by loopback tests.

Preflight requires exact panel/core versions, running Xray, enabled inbound,
VLESS/Reality/XHTTP capability and canonical test-email NOT FOUND. HTTP 404 of a
missing API route is not accepted as proof of email absence. An existing test
email blocks create; no automatic reuse/update/delete.

Create uses runtime XUIAdapter.add_client with one explicit membership. Canonical
reconciliation confirms persisted fields; a validated add/update response with
obj=null or explicit nodePending=false allows CREATE_PASS. True/None requires review. Later read-only
verification cannot promote uncertain activation or unlock update.

Verify stages check canonical identity/expiry/settings, numeric traffic separately,
panel subURI and share-link UUID/XHTTP/Reality/empty flow without printing URLs.
They allow the source-confirmed read-only POST /panel/api/setting/all, with {} body,
and GET /clients/subLinks/{state-subId}; these are not in preflight's allowlist.
Canonical GETs can repeat as distinct pre-check/reconciliation operations, not retries.

An optional separately approved `--fetch-subscription` on a verification stage
performs one HTTPS GET to the exact canonical-subId URL. It sends no panel Bearer
or cookies, follows no redirect, bounds the body to 1 MiB, checks plain/base64 VLESS
links in memory and saves no body. This fetch can update panel access/HWID bookkeeping;
it is NOT implicitly authorized by a panel-read approval. No Xray handshake occurs.
Default verification reports HTTP subscription fetch NOT RUN; this is still required
before claiming complete functional subscription success.

Update preserves the full serializer contract and exact membership. It changes
only expiry by +1 day. A hash of the preserved client payload (excluding expiry)
detects edits across stages, in addition to adapter stale snapshot checks.

## Source-verified cleanup

[v3.8.5 client controller](https://github.com/MHSanaei/3x-ui/blob/v3.8.5/internal/web/controller/client.go):
POST /panel/api/clients/del/{email}, no request body, no query parameters here.
keepTraffic=1 is optional upstream; the smoke omits it (false).
Response is jsonMsg success/msg/obj=null, without a nodePending confirmation.

## Patch 6D.1 local correction (no production requests)

The add/update normal response is success=true,obj=null: pendingNodeObj(false)
returns nil, so a received valid response means node_pending=False. A true flag
means pending work. Other object shapes are unsupported and conservatively
reconcile persistence without establishing activation. Lost, malformed or rejected
responses remain node_pending=None even if canonical GET matches.

Future CLI mutations record safe response metadata in the separate gitignored
.xui-write-smoke-outcome.json: response_received, HTTP status, envelope validity,
success, obj shape and nodePending presence/boolean value. No body, token or client
credentials are stored. This artifact is produced only for a newly executed
mutation stage; this patch did not run any production stage or create that file.

Historical CREATE_REVIEW remains unchanged: the original response metadata was
not recorded, so its classification cannot be reconstructed from persistence.
The UPDATE journal gate remains closed; a separate explicit manual recovery
decision and stage authorization are required before any controlled trial.

[Delete/DeleteByEmail service](https://github.com/MHSanaei/3x-ui/blob/v3.8.5/internal/web/service/client_crud.go):
delete fans out to all client attachments, then removes canonical identity, links,
HWID and traffic bookkeeping; partial failure may leave applied changes.
It is not a detach-one-inbound API. Cleanup therefore re-reads and requires exact
state email/UUID/subId and membership (6,) before the only deletion POST.

Deletion is smoke-local; no delete method is added to the runtime adapter.
After any response/error, canonical GET is attempted once: NOT FOUND confirms
persistence deletion only, activation UNKNOWN. Still present/unreadable requires
manual review. No retry even on a later cleanup command. Separate --verify-cleanup
checks absence without mutation.

## Remaining operational limitations

Backend has no UUID-conditioned atomic delete or client compare-and-set. An external
panel edit between fresh GET and POST remains a race: do not edit/attach this
synthetic client during the trial. The guard cannot make the panel atomic.
Journal loss or uncertain activation needs manual reconciliation; do not edit the
journal to force a retry. Canonical absence is not proof of Xray/node removal.
Network/TLS/subscription routing and VPN handshake have not been tested live here.

## Local tests

Locked Python 3.12.12: full unittest suite 213 passed, 0 skips (30 new smoke tests).
poetry check --lock, pip check and git diff --check passed; Poetry emits existing
metadata deprecation warnings. Loopback tests cover stages, counts, payload,
preservation, ambiguity, cleanup identity/membership refusal, no transport replay
and safe output. No production response or credential is used as a fixture.
The suite includes journal failures
before/after POST: counters remain available and durable ATTEMPTED blocks replay.
