# Patch 6E — 3x-ui v3.9.0 delta audit

Source audit performed 2026-10-05 with official tagged source and synthetic HTTP.
The source audit itself performed zero production requests. Later separately
authorized live evidence is recorded in [smoke report](3xui-live-write-smoke.md).

## Versions / evidence

| Tag | Commit |
|---|---|
| v3.8.5 | 7ef22f94c950ff09f0870e2295fa65ad5968742c |
| v3.9.0 | 3cd4bf504c3cd8ea9b1c1fdb032a9796c5c43ddb |

[Official release](https://github.com/MHSanaei/3x-ui/releases/tag/v3.9.0) was published
2026-10-03 and targets Xray-core 26.9.30. Subsequent separately approved reads confirmed production panel 3.9.0 and
running Xray 26.9.30; these live findings are separate from source verification. Compared individual files
from both tags using the recursive tag tree. GitHub compare returned a capped
300-file listing, so it was not treated as a complete inventory.

## Authentication: UNCHANGED

[API controller](https://github.com/MHSanaei/3x-ui/blob/v3.9.0/internal/web/controller/api.go),
[CSRF middleware](https://github.com/MHSanaei/3x-ui/blob/v3.9.0/internal/web/middleware/security.go)
and session implementation are byte-identical. Valid Bearer sets api_authed and
bypasses session CSRF. Invalid Bearer without a session: 401; disallowed scope:
403; unknown route/base path: 404. Server session fallback remains, but the shop's
TOKEN mode never attempts it. Custom panel base-path routing remains supported.
Admin scope is required for the complete shop read/settings surface. Monitor
permits status; node-sync permits status/inbounds and selected writes, not
canonical/traffic/subLinks/settings reads.

[Token service](https://github.com/MHSanaei/3x-ui/blob/v3.9.0/internal/web/service/panel/api_token.go)
changes CLI rotation to preserve scope/expiry and reject expired tokens. Bearer
wire auth is unchanged; rotation must not be assumed to confer admin privileges.

## API matrix

All paths are relative to XUI_HOST, including its custom base path.

| HTTP / endpoint | Contract delta | Status |
|---|---|---|
| GET /panel/api/server/status | Same fields; CurrentStatus replaces nullable cold-start LastStatus | COMPATIBLE |
| GET /panel/api/inbounds/list | New excludeFromSub boolean | COMPATIBLE WITH CAVEAT |
| GET /panel/api/clients/get/{email} | Same wrapper and full inboundIds; client adds resetWeekday | COMPATIBLE WITH CAVEAT |
| GET /panel/api/clients/traffic/{email} | Same object/null contract; extra resetWeekday | COMPATIBLE |
| GET /panel/api/clients/subLinks/{subId} | Still share-link string array; hidden inbounds omitted | COMPATIBLE WITH CAVEAT |
| POST /panel/api/setting/all | Same read-only settings/subURI contract | COMPATIBLE |
| POST /panel/api/clients/add | JSON client + inboundIds; optional traffic import field added | COMPATIBLE WITH CAVEAT |
| POST /panel/api/clients/update/{email} | Same flat Client + limitHwid, optional inboundIds query filter; resetWeekday added | COMPATIBLE WITH CAVEAT |
| POST /panel/api/clients/del/{email} | No body; keepTraffic=1 optional; same null response | COMPATIBLE |

Sources: [client controller](https://github.com/MHSanaei/3x-ui/blob/v3.9.0/internal/web/controller/client.go),
[create payload](https://github.com/MHSanaei/3x-ui/blob/v3.9.0/internal/web/service/client.go),
[status service](https://github.com/MHSanaei/3x-ui/blob/v3.9.0/internal/web/service/server.go),
[settings controller](https://github.com/MHSanaei/3x-ui/blob/v3.9.0/internal/web/controller/setting.go).
No new CRUD endpoint or credential is required for XHTTP.

## Patch 6D.1 response contract: UNCHANGED

[Response helpers](https://github.com/MHSanaei/3x-ui/blob/v3.9.0/internal/web/controller/util.go)
are byte-identical. Add/update still call pendingNodeObj: false yields nil, true
yields {nodePending:true}. No other successful object is returned by those handlers.
Explicit false is accepted for compatibility, but the helper does not emit it.

| Response + successful canonical reconciliation | node_pending |
|---|---|
| Received 2xx, success=true, obj=null, add/update | False |
| Received 2xx, success=true, obj={nodePending:true} | True |
| Unknown object, invalid/missing envelope, API error or lost response; persisted desired state | None |
| Existing/already-desired shortcut without POST | None |

Delete uses jsonMsg, not pendingNodeObj: null acknowledges its handler result,
not absence of remote pending work. Cleanup activation remains UNKNOWN after
canonical absence. No mutation retry/redirect/auth fallback is introduced.
Persistence confirmation is distinct from node acknowledgement and VPN handshake.

## Model / preservation delta

[Model structs](https://github.com/MHSanaei/3x-ui/blob/v3.9.0/internal/database/model/model.go)
add Client/ClientRecord resetWeekday (int; 0 disabled, 1 Monday through 7 Sunday),
Inbound excludeFromSub (bool), and Host cipherSuites. UUID/email/subId, ClientInbound
membership and NodeID meanings remain intact. Canonical record id is numeric;
write Client.id is the credential UUID. expiryTime is epoch milliseconds, totalGB
raw bytes (0 unlimited); limitIp and limitHwid remain distinct. There is no new
client CAS/revision field; existing timestamps are server-owned.

[Traffic struct](https://github.com/MHSanaei/3x-ui/blob/v3.9.0/internal/xray/client_traffic.go)
adds resetWeekday and internal non-JSON TUIC ingestion identifiers. Traffic id
never supplies canonical UUID or membership. resetCount/lastOnline/lastSubFetch
are accounting fields, not preservation payload fields.

Minimal shop fix: preserve and validate resetWeekday when present, including
desired-state and stale-snapshot comparison. It remains optional for v3.8.5;
absent old responses do not acquire a fabricated weekly schedule. Existing
identity precedence, field allowlist, memberships and server-metadata exclusion
remain unchanged. New create omits weekly mode (backend default 0).

## Lifecycle / concurrency

[CRUD service](https://github.com/MHSanaei/3x-ui/blob/v3.9.0/internal/web/service/client_crud.go)
validates weekly mode: positive resetWeekday cannot combine with positive reset
or resetDay. Update still targets existing attachments; inboundIds query is a
filter, not replacement ownership. Full client configuration is still required
for safe preservation. Fixed-duration shop renewal remains update-by-email;
expiry-only extension does not imply traffic reset or another purchase.

[New commit helper](https://github.com/MHSanaei/3x-ui/blob/v3.9.0/internal/web/service/inbound_settings_commit.go)
three-way merges base/ours/current inbound JSON at commit, matching clients by
email and preserving unchanged fields/concurrent additions/removals. Client ops
no longer save an entire stale inbound row. Inbound-form saves preserve stored
clients and enable instead of authoritatively replacing lifecycle fields.
This protects server-internal traffic-tick races, not a stale full payload sent
after our last GET. Keep the fresh snapshot guard; there is no HTTP CAS guarantee.

[renewalPreview](https://github.com/MHSanaei/3x-ui/blob/v3.9.0/internal/web/service/client_renewal_preview.go)
is a calculation-only POST for interval/monthly/weekly panel-local schedules and
catch-up suggestions. It neither renews a client nor replaces paid fixed-duration
extension. No new renewal mutation route appears in the client controller.
Do not automatically switch endpoints or enable panel auto-renew on shop clients.

## Counter / node behavior

[Traffic service](https://github.com/MHSanaei/3x-ui/blob/v3.9.0/internal/web/service/client_traffic.go)
zeroes counters before re-enabling depleted clients; single/bulk/global resets
enqueue durable node work. [Reset queue](https://github.com/MHSanaei/3x-ui/blob/v3.9.0/internal/web/service/node_reset_queue.go)
keeps unacknowledged work and deletes rows only when the accepted generation still
matches. [Sync job](https://github.com/MHSanaei/3x-ui/blob/v3.9.0/internal/web/job/node_traffic_sync_job.go)
delivers resets before fetching node snapshots.

[Node merge](https://github.com/MHSanaei/3x-ui/blob/v3.9.0/internal/web/service/inbound_node.go)
freezes lifecycle verdicts while a client owes reset work; usage may continue.
Weekly schedule/count propagation expands existing renewal handling.
[Traffic renewal](https://github.com/MHSanaei/3x-ui/blob/v3.9.0/internal/web/service/inbound_traffic.go)
adds weekly catch-up, canonical traffic ingestion and post-commit MTProto quota
reset. Cross-panel quota-window clearing already existed in v3.8.5; release-note
claims are not all newly introduced wire contracts.

[Remote runtime](https://github.com/MHSanaei/3x-ui/blob/v3.9.0/internal/web/runtime/remote.go)
resolves remote reset inbound ID by tag and adds bulkResetTraffic. The unchanged
node-sync scope allowlist lacks that bulk route: large queued batches using
node-sync credentials may be denied 403. Review this upstream combination when
using nodes. The admin-token shop does not issue traffic resets in this patch.

## XHTTP / subscription

VLESS Reality XHTTP remains compatible with shop flow="". clientWithInboundFlow,
relevant [protocol helpers](https://github.com/MHSanaei/3x-ui/blob/v3.9.0/internal/web/service/inbound_util.go)
and [Xray inbound model](https://github.com/MHSanaei/3x-ui/blob/v3.9.0/internal/xray/inbound.go)
retain the relevant logic. Keep verified empty global settings.flow, encryption/
decryption policy, enabled inbound, supported XHTTP modes and no Vision for XHTTP.
TCP TLS/Reality regression policy remains unchanged.

[Subscription generation](https://github.com/MHSanaei/3x-ui/blob/v3.9.0/internal/sub/service.go)
omits excludeFromSub inbound links while retaining usage accounting. subLinks
still returns share links, not the HTTP subscription endpoint. subURI/explicit
HTTPS base override and custom path handling remain the shop URL contract.
subId may identify several clients; it is not a globally unique client identity.
XHTTP share serialization needs no special shop URL rules.

## Panel migrations

[Database bootstrap](https://github.com/MHSanaei/3x-ui/blob/v3.9.0/internal/database/db.go)
adds weekly columns on clients/client_traffics, backfills NULL to 0, adds
excludeFromSub, node_pending_resets and tuic_traffic_receipts. One-time XDNS and
WireGuard outbound rewrites target the core format. No shop Alembic migration
is required or run. Weekly clients require compatible node binaries; older
cleanup/downgrade behavior can mishandle weekly-only schedules.

## Readiness / boundaries

Source/local contracts and a separately approved controlled v3.9.0 panel lifecycle
are verified. The synthetic client completed create/read/update/read/delete and
is removed; see [safe live evidence](3xui-live-write-smoke.md). New production
requests still require separate explicit authorization. No HTTP subscription
fetch, VPN handshake, Telegram E2E or payment pilot was performed.

The runtime ServerPoolService TARGET_PANEL_VERSION remains 3.8.5; this audit and
standalone 3.9.0 smoke do not change its startup version gate. A separately reviewed
runtime target change is needed before normal shop startup against 3.9.0.
Historical CREATE_REVIEW remains historical; it is not reused or promoted.
