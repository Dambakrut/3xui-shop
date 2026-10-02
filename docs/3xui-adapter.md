# 3x-ui 3.8.5 adapter — Patch 6B.2b

Target: MHSanaei/3x-ui v3.8.5; reported production Xray Core 26.9.30.
Source/local tests only. No production requests or live writes performed.

## Runtime integration status

READ PATH: MODERN XUIAdapter

WRITE PATH: MODERN XUIAdapter

LEGACY py3xui: NOT USED FOR PANEL RUNTIME OPERATIONS

LIVE PRODUCTION WRITE: NOT TESTED

ServerPoolService owns one adapter/session per server. Startup remains read-only:
authenticate, require panel 3.8.5/running Xray, validate enabled configured inbound
and known protocol. Failed candidates, refreshed connections and shutdown sessions
are closed. Existing server selection is preserved; no first-inbound fallback.
py3xui 0.3.2 remains locked for historical audit tests, with no runtime imports.

## Authentication

SESSION default: GET /csrf-token, retain 3x-ui cookie/token, POST /login JSON
username/password/optional twoFactorCode with X-CSRF-Token. Every authenticated
POST sends CSRF. Token is not rotated on login. No mutation replay on auth failure.
Adapter supports 2FA code; shop does not configure rotating 2FA credentials.

TOKEN: explicit XUI_AUTH_MODE=token and XUI_API_TOKEN. Bearer header, no login or
CSRF. Shop requires admin scope: monitor allows status only; node-sync lacks the
canonical reconciliation/settings reads. Legacy XUI_TOKEN is never Bearer.
[Scope routes](https://github.com/MHSanaei/3x-ui/blob/v3.8.5/internal/web/controller/api.go),
[CSRF middleware](https://github.com/MHSanaei/3x-ui/blob/v3.8.5/internal/web/session/csrf.go).

## Endpoints

All paths include configured panel web base path. JSON envelopes: success/msg/obj.

| Method | HTTP route | Return |
|---|---|---|
| authenticate | GET /csrf-token, POST /login (SESSION only) | None |
| get_server_status | GET /panel/api/server/status | XUIServerStatus |
| list_inbounds/get_inbound | GET /panel/api/inbounds/list, exact ID filter | XUIInboundSummary |
| get_client | GET /panel/api/clients/get/{email} | XUIClient or XUINotFoundError |
| get_client_traffic | GET /panel/api/clients/traffic/{email} | XUIClientTraffic or None |
| get_subscription_links | GET /panel/api/clients/subLinks/{subId} | Share links, NOT HTTP subscription URL |
| get_subscription_base_url | POST /panel/api/setting/all (read-only) | Validated explicit subURI |
| add_client | POST /panel/api/clients/add | XUIWriteResult |
| update_client | POST /panel/api/clients/update/{email} | XUIWriteResult |

[Controller](https://github.com/MHSanaei/3x-ui/blob/v3.8.5/internal/web/controller/client.go):
add JSON {client: model.Client including limitHwid, inboundIds: [configured ID]}.
Update is flat full model.Client including limitHwid, not PATCH. Optional update
query inboundIds filters applied inbounds, does not change memberships. Shop omits
that filter, updating shared state across preserved memberships. No attachment,
delete, inbound mutation or reset-traffic API is exposed by this adapter.

## Identity and multi-inbound isolation

Canonical client.uuid is credential UUID. Canonical record_id and traffic.id are
independent numeric keys. Email is current shop Telegram ID string. UUID must equal
User.vpn_id; configured ID must be present in all returned inboundIds. Create only
attaches configured inbound. Update cannot change membership set. Shared expiry
and enable changes affect all memberships; disabled/non-VLESS attachments fail
closed. Membership preservation does not imply per-inbound quota/expiry isolation.

## Write model and field preservation

XUIClientWrite explicitly carries UUID/email, memberships, expiry milliseconds,
quota bytes, IP/HWID limits, tgId/subId, enable, flow/comment/reset. Raw preservation
uses a checked model.Client allowlist; identity overrides raw, server IDs/timestamps
are excluded. Missing preservation fields fail closed. allowedIPs canonical JSON
text is decoded to wire array; reverse remains object/null. Types are validated.
Renewal preserves quota, subId, HWID, flow, comment, resets/calendar, traffic reset,
group, reverse and credential fields. Future unknown fields are not blindly echoed.

[Model](https://github.com/MHSanaei/3x-ui/blob/v3.8.5/internal/database/model/model.go),
[CRUD semantics](https://github.com/MHSanaei/3x-ui/blob/v3.8.5/internal/web/service/client_crud.go).

Update re-reads canonical state before POST. Changed preservation snapshot fails
closed unless desired state is already present. Backend has no compare-and-set;
external edits between final GET and POST remain a race requiring operational
isolation. Canonical read cannot prove successful fanout to remote Xray nodes.

## Create/renewal policy

Create: User.vpn_id UUID/subId, configured membership only, epoch-ms expiry,
quota 0 means unlimited bytes, enabled by default. Supported shop creation:
VLESS TCP + TLS/Reality + Vision enabled. Unsupported protocol/transport/security
or disableFlow fails closed. Conditional XHTTP support in upstream is outside
this patch. [Flow source](https://github.com/MHSanaei/3x-ui/blob/v3.8.5/internal/web/service/inbound_protocol.go).
limitIp means IP limit, NOT physical device count; HWID is separate. Existing UI
terminology remains. Server assignment follows verified canonical persistence.

Positive active expiry extends from expiry; expired positive expiry extends from
now; change-subscription starts from now. Zero unlimited/negative delayed-start
require manual review. Renewal does not overwrite quota/flow. Legacy optional
quota/flow override arguments are rejected when not the preserved default policy.

## Ambiguous writes and reconciliation

Automatic mutation retries: NONE. Pre-read existing email: identical intended
state/UUID/membership -> success without POST; mismatch -> review, never duplicate
or implicit attach. Every POST is followed by canonical desired-state verification.
Timeout/drop/malformed response/error envelope/HTTP error may follow partial commit:
GET only, never resend POST. Matching state returns reconciled result with
node_pending=None (activation unknown); missing/mismatched state raises
XUIReconciliationError, a subclass of XUIAmbiguousWriteError.

Persistence confirmation != node activation confirmation.
XUIWriteResult.success means canonical persistence verified, not node activation.
node_pending=False means a valid write response explicitly returned nodePending=false;
True means a valid write response explicitly returned nodePending=true.
None means canonical persistence confirmed but activation unknown: no-POST existing
or already-desired shortcuts, success with obj=null, or a lost mutation response.
VPNService accepts only False for completed provisioning;
True/None raises uncertainty. Existing payment state machine sends uncertainty to
REVIEW_REQUIRED and duplicate callback does not replay writes. No payment code
changed. Manual recovery must inspect panel/order; expiry intent is not separately
durable, and exactly-once writes across crashes are not claimed. Notification and
referral post-completion crash windows remain unchanged.

## HTTP subscription URL

subLinks returns protocol share links (vless:// etc.), not HTTP subscription URL.
[Official BuildURLs](https://github.com/MHSanaei/3x-ui/blob/v3.8.5/internal/sub/service.go)
uses subURI if set, else settings/host context. Public reverse proxy cannot safely
be inferred from panel host.

Set XUI_SUBSCRIPTION_BASE_URL=https://sub.example/custom/sub/ or leave empty to read
explicit panel subURI via read-only POST /setting/all. Require HTTPS, hostname,
trailing-slash path, no credentials/query/fragment/invalid port/control characters.
Append validated canonical subId (create uses User.vpn_id; renewal preserves it). No
legacy /user/ fallback. Override is shared across pool; for distinct subscription
hosts configure subURI per panel. Validation does not prove public reachability.

## Errors/TLS/secrets

Typed auth/authorization/not-found/protocol/API/transport errors remain; mutation
uncertainty adds ambiguous/reconciliation errors. Exception messages contain no
response body, credentials or URL. TLS verification ON, no ssl=False, redirects,
environment proxy trust or retries. Request timeout bounds each operation. Sessions
close explicitly. Raw/secrets hidden from repr; no full keys/share URLs logged.

## Local verification and remaining work

Loopback HTTP tests cover auth/wire body, preservation, memberships, stale snapshot,
nodePending, timeout/drop/malformed/error reconciliation, no retries and logging.
Service tests cover expiry/ownership/server assignment, REVIEW_REQUIRED and URL.
Fixtures are synthetic source-derived v3.8.5 responses, not production captures.

Later separate stages: controlled live read smoke; dedicated SHOP-TEST inbound;
controlled create/verify/update expiry/verify/optional cleanup; Telegram E2E;
Stars real payment pilot. No live testing in this patch.
