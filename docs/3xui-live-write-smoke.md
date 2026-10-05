# Controlled 3x-ui client lifecycle smoke

## Completed v3.9.0 trial - 2026-10-05

Target: 3x-ui 3.9.0, Xray Core 26.9.30 running, Bearer admin authentication,
inbound 6 enabled with VLESS / Reality / XHTTP, flow="", disableFlow=false,
excludeFromSub=false. No Telegram/payment runtime was started.

All stages had separate operator approval. The test client was deleted.
Production actions are finished; this report does not authorize another run.

| Stage | Result | GET | POST |
|---|---|---:|---:|
| Preparation / dry-run | PREPARED, network requests 0 | 0 | 0 |
| Preflight | PREFLIGHT_PASS; synthetic client absent | 3 | 0 |
| Create | CREATE_PASS; canonical persistence confirmed | 5 | 1 |
| Verify create | VERIFY_CREATE_PASS | 5 | 0 |
| Update | UPDATE_PASS; only expiry changed | 5 | 1 |
| Verify update | VERIFY_UPDATE_PASS | 5 | 0 |
| Cleanup | CLEANUP_PASS; canonical NOT FOUND | 2 | 1 |
| Total for this lifecycle | Complete | 25 | 3 |

The earlier independent v3.9.0 status/inbound smoke made two GET requests.
An earlier GET for the historical, manually removed client returned not-found;
that is not an API regression and is not part of this new lifecycle.

Create and update each received HTTP 200, a valid success=true envelope and
obj=null. Under the verified pendingNodeObj contract this means node_pending=false.
Both canonical reconciliation reads confirmed the intended persisted state.
Read verification confirmed exact UUID/subId, memberships [6], flow="", enable=true,
limitIp=1, limitHwid=0, totalGB=0, resetWeekday integer 0 and preservation hash.
Expiry changed from 2026-10-08T14:46:34.486Z to 2026-10-09T14:46:34.486Z.

Traffic was readable, enabled, with up/down/total all zero. Numeric traffic ID
was never used as credential identity. Each verification found one matching
VLESS / XHTTP / Reality share link with exact UUID and no Vision flow. Full links
and production payloads were not stored. HTTP subscription fetch and VPN handshake
were NOT RUN; panel/share-link success does not prove end-to-end VPN connectivity.

Cleanup made only canonical GET, one POST delete, then canonical GET. Its valid
HTTP 200 success=true,obj=null acknowledges handler success. NOT FOUND confirms
persistence deletion; node activation/synchronization remains UNKNOWN.
The ignored new journal is CLEANUP_PASS. No automatic further stage is authorized.

## Historical v3.8.5 trial

The old create persisted, but original response metadata was not retained and its
journal remains CREATE_REVIEW. It was never automatically reclassified by 6D.1.
The operator manually deleted that old client. The new v3.9.0 lifecycle has a
separate identity/journal and does not use historical ambiguity as evidence.

## Endpoint and approval contract

All paths use a placeholder, not a production hostname/base path or identity.

```text
GET /<base>/panel/api/server/status
GET /<base>/panel/api/inbounds/list
GET /<base>/panel/api/clients/get/{smoke-email}
GET /<base>/panel/api/clients/traffic/{smoke-email}
GET /<base>/panel/api/clients/subLinks/{state-subId}
POST /<base>/panel/api/clients/add
POST /<base>/panel/api/clients/update/{smoke-email}
POST /<base>/panel/api/clients/del/{smoke-email}
```

Run scripts/xui_live_write_smoke.py manually in the locked Python 3.12 environment.
Required env: XUI_HOST, XUI_AUTH_MODE=token, XUI_API_TOKEN, XUI_INBOUND_ID=6.
.env must be ignored and untracked. --dry-run generates the new identity only if
its separate state file does not exist. Missing/corrupt state blocks network stages.
Network stages require matching --confirm-stage plus separate user approval;
one command never chains mutations. --dry-run alone performs no network operation.

The fresh state pins panel 3.9.0, exact prefix shop-smoke-390-, uuid4 credentials,
inbound 6, initial expiry +3 days, update +1 day, and resetWeekday=0 (no weekly mode).
Preflight checks running core, exact version, enabled compatible inbound, empty
resolved/global flow, excludeFromSub=false and canonical client absence.

Every stage has an exact identity-bound method/path allowlist. At most one armed
mutation is allowed; its body is compared with the expected serializer output.
Before update/delete, canonical identity and membership are checked. Update
changes only expiry and preserves all allowlisted fields; snapshot/preservation
hash checks reject intervening edits. Live cleanup additionally checked flow,
enable, weekday and updated expiry before deletion.

TLS verification stays enabled; redirects, cookies, environment proxies,
session login/CSRF, auth fallback and mutation retries are disabled. Locked aiohttp
implicit idempotent retries are disabled for the sequential requests too.
Distinct canonical pre-check/reconciliation GETs are not request retries.

Verification normally uses GET only: safety reads, canonical, traffic, subLinks.
An optional separately approved --fetch-subscription can additionally read
settings via POST /panel/api/setting/all and GET the HTTP subscription URL.
That option was not used live. The HTTP subscription read can update access/HWID
bookkeeping and must not be implied by approval for panel reads.

## Response contract and safe instrumentation

Official v3.8.5/v3.9.0
[helpers](https://github.com/MHSanaei/3x-ui/blob/v3.9.0/internal/web/controller/util.go)
and [client handlers](https://github.com/MHSanaei/3x-ui/blob/v3.9.0/internal/web/controller/client.go):
add/update use pendingNodeObj(false) -> nil, pendingNodeObj(true) -> {nodePending:true}.
Received valid success with obj=null means false. Explicit true means review.
Unknown objects, lost/malformed/error responses plus canonical desired state mean
persistence confirmed but node_pending=None; no second POST is sent.

Delete uses jsonMsg, not pendingNodeObj: null does not establish absence of node
pending work. It deletes canonical client and all attachments, not one membership.
The smoke therefore requires exact membership [6] before the only delete request.
Ambiguous delete is reconciled with one canonical GET, never replayed.

Safe metadata only: response_received, HTTP status, envelope_valid, success,
obj shape, nodePending presence/value and valid_mutation_response. No raw body,
API token, cookies, CSRF, full credential UUID/subId or share link is committed.

Ignored files: .xui-write-smoke-390-state.json, its lock/temp file and
.xui-write-smoke-390-outcome.json. Historical .xui-write-smoke-state.json and
outcome also remain ignored. Journals retain only local synthetic identity,
planned expiries, panel binding/preservation hashes and stage. Durable intent
before mutation blocks replay after crash. Keep these files locally as evidence;
they are not fixtures or commit inputs. CLEANUP_PASS is terminal and blocks cleanup replay.

## Limits and local regression

There is no panel UUID-conditioned delete or HTTP compare-and-set; an external
edit after our fresh GET remains a race. Do not edit the synthetic client during
an approved trial. Canonical absence is not Xray/node removal confirmation.
HTTP subscription routing, VPN handshake, Telegram E2E and payment pilot remain
outside this completed panel lifecycle smoke.

Tests use synthetic loopback responses only; v3.8.5 fixtures are retained and
v3.9.0 fixtures cover weekday preservation, visibility and response contracts.
See the commit review report for the final locked-environment suite results.
