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
and enable changes affect all memberships; disabled/unsupported attachments fail
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
quota 0 means unlimited bytes, enabled by default. Creation and renewal use the
explicit capability matrix below; unknown combinations fail closed.
limitIp means IP limit, NOT physical device count; HWID is separate. Existing UI
terminology remains. Server assignment follows verified canonical persistence.

Positive active expiry extends from expiry; expired positive expiry extends from
now; change-subscription starts from now. Zero unlimited/negative delayed-start
require manual review. Renewal does not overwrite quota/flow. Legacy optional
quota overrides are rejected; an explicit renewal flow must equal the canonical
flow. No flow conversion is performed during renewal.

## XHTTP capability policy — 3x-ui v3.8.5 / Xray v26.9.30

| Protocol | Security | Network | Flow | Shop support |
|---|---|---|---|---|
| VLESS | Reality | TCP | xtls-rprx-vision | Supported; creation default |
| VLESS | TLS | TCP | xtls-rprx-vision | Supported; creation default |
| VLESS | Reality / TLS | TCP | empty | Supported with verified empty inbound settings.flow |
| VLESS | Reality | XHTTP | empty | Supported; creation default |
| VLESS | Reality | XHTTP | xtls-rprx-vision | Rejected by shop; upstream allows it conditionally with VLESS encryption |
| VLESS | TLS / other | XHTTP | any | Outside shop policy; rejected |
| other / unknown | any | any | any | Rejected |

This is a shop policy, not an exhaustive Xray support matrix. TCP retains the
disableFlow=false requirement. XHTTP accepts either boolean disableFlow value
because its client flow is empty; malformed values fail closed.

3x-ui's [inboundCanEnableTlsFlow](https://github.com/MHSanaei/3x-ui/blob/v3.8.5/internal/web/service/inbound_protocol.go)
permits Vision on TCP+TLS/Reality, and on XHTTP only with VLESS-level encryption
(encryption/decryption settings). Reality alone is not that encryption.
[clientWithInboundFlow](https://github.com/MHSanaei/3x-ui/blob/v3.8.5/internal/web/service/client_crud.go)
clears flow when disableFlow is true or an attachment cannot enable it. This
switch does not disable clients with empty flow. The shop deliberately supports
only empty flow for XHTTP; it does not configure ML-KEM or infer its validity.

[Xray VLessInboundConfig.Build](https://github.com/XTLS/Xray-core/blob/v26.9.30/infra/conf/vless.go)
accepts empty/Vision client flow and UUID credentials. Empty client flow inherits
inbound settings.flow: the shop requires readable settings with absent/empty flow
before accepting an empty client flow. Nonempty global flow fails closed, even
when disableFlow=true. No new credential is required for XHTTP. The udp443 suffix
is an outbound option, not an accepted inbound client flow.

[StreamConfig.Build](https://github.com/XTLS/Xray-core/blob/v26.9.30/infra/conf/transport_internet.go)
maps xhttp to splithttp and permits Reality on this transport. XHTTP settings must
be present as an object; mode must be empty, auto, stream-one, stream-up or packet-up,
as handled by the [XHTTP server](https://github.com/XTLS/Xray-core/blob/v26.9.30/transport/internet/splithttp/hub.go).
Host/path/extra/padding remain panel transport configuration, not client fields.
The shop neither copies them into client requests nor mutates them. This guard
does not validate every transport setting or prove a successful VPN handshake.

The [canonical client controller](https://github.com/MHSanaei/3x-ui/blob/v3.8.5/internal/web/controller/client.go)
uses the same JSON add/update routes for XHTTP: add {client, inboundIds}, update
flat full client. Expiry milliseconds, quota bytes, enable, IP/HWID limits and subId
remain in that client contract; memberships are preserved. Every attachment is
validated against the actual canonical flow before update. A shared empty-flow
TCP/XHTTP client is allowed only when all attachments pass; Vision/XHTTP and mixed
unsupported attachments are rejected without a mutation.

[Subscription service](https://github.com/MHSanaei/3x-ui/blob/v3.8.5/internal/sub/service.go)
generates XHTTP share-link network/path/host/mode/extra parameters in
applyShareNetworkParams/applyXhttpExtraParams. HTTP BuildURLs remains transport
independent. No shop URL rule changes: panel subURI or the explicit HTTPS base is
used, then canonical subId is appended. Correct source generation does not prove
compatibility of every user VPN application with XHTTP extras.

tests/fixtures/xui/v3_8_5/inbound_xhttp.json is synthetic, matching the known
production combination and inbound ID, with invented transport details. It is
not a production payload. No production request was performed in this patch.
Historical 6C NOT READY described the previous TCP-only guard and remains an
unchanged historical record. The next separate stage is an authorized SHOP-TEST
write trial; activation, subscription reachability and VPN handshake remain unverified.

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
