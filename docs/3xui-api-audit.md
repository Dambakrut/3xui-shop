# Patch 6A — 3x-ui API Compatibility Audit

Дата проверки: 2026-10-02. Это статический аудит официальных исходников и локальные wire tests; проверка production не выполнялась. Runtime не изменён.

## Versions and evidence

- Shop: `feature/3xui-api-audit`, HEAD `c9394e97e7a4484eca6ae6d2372711e0b9c0b730` (Patch 5).
- Установленный пакет: py3xui **0.3.2**, исходники `.venv-runtime-test/Lib/site-packages/py3xui`.
- Latest stable MHSanaei/3x-ui: **v3.8.5**, release 2026-09-16, исходный commit `7ef22f94c950ff09f0870e2295fa65ad5968742c` (annotated tag dereferenced).
- Production panel version: **UNKNOWN**. Версия Xray Core **26.9.30** сообщена владельцем, самостоятельно не проверена.
- Latest py3xui: **0.7.0**; отдельные официальные исходники просмотрены, пакет не установлен и не обновлён.
- Исторические v2.9.4 и v3.0.0 ещё содержат legacy inbound client routes. Первый release перехода здесь не установлен; нельзя приписывать изменение точно v3.0.0.

Первичные источники (при расхождении OpenAPI и backend приоритет у backend):

1. [Официальный release v3.8.5](https://github.com/MHSanaei/3x-ui/releases/tag/v3.8.5).
2. [OpenAPI v3.8.5](https://github.com/MHSanaei/3x-ui/blob/v3.8.5/docs/public/openapi.json), `info.version = 3.x` — это не точная версия установленной панели.
3. [Client routes/controllers](https://github.com/MHSanaei/3x-ui/blob/v3.8.5/internal/web/controller/client.go), [inbound controller](https://github.com/MHSanaei/3x-ui/blob/v3.8.5/internal/web/controller/inbound.go).
4. [Auth/API scopes](https://github.com/MHSanaei/3x-ui/blob/v3.8.5/internal/web/controller/api.go), [login](https://github.com/MHSanaei/3x-ui/blob/v3.8.5/internal/web/controller/index.go), [CSRF](https://github.com/MHSanaei/3x-ui/blob/v3.8.5/internal/web/session/csrf.go).
5. [Backend models](https://github.com/MHSanaei/3x-ui/blob/v3.8.5/internal/database/model/model.go), [client CRUD](https://github.com/MHSanaei/3x-ui/blob/v3.8.5/internal/web/service/client_crud.go), [inbound application](https://github.com/MHSanaei/3x-ui/blob/v3.8.5/internal/web/service/client_inbound_apply.go), [traffic](https://github.com/MHSanaei/3x-ui/blob/v3.8.5/internal/web/service/inbound_traffic.go).
6. [Subscription routes](https://github.com/MHSanaei/3x-ui/blob/v3.8.5/internal/sub/controller.go), [subscription service](https://github.com/MHSanaei/3x-ui/blob/v3.8.5/internal/sub/service.go).
7. [py3xui official repository](https://github.com/iwatkot/py3xui), [0.3.2 distribution](https://pypi.org/project/py3xui/0.3.2/), [0.7.0 client implementation](https://github.com/iwatkot/py3xui/blob/0.7.0/py3xui/async_api/async_api_client.py).

## Shop surface

| Shop location | Реальная операция |
|---|---|
| `app/bot/services/server_pool.py`: `_add_server`, `validate_configured_inbound`, `sync_servers` | Отдельный AsyncApi на сервер; login, list inbounds, проверка существования configured ID; локальная online bookkeeping |
| `app/__main__.py`: `main` | Startup sync pool, без создания/изменения клиентов на этом этапе |
| `app/bot/services/vpn.py`: `is_client_exists`, `get_client_data` | Lookup по email = decimal Telegram ID; traffic/expiry чтение |
| `VPNService.get_limit_ip` | List inbounds; только configured ID; совпадение email и UUID в settings.clients |
| `VPNService.create_client` | Add в configured inbound; UUID/subId из User.vpn_id; server_id записывается после успешного add |
| `VPNService.update_client` | Lookup, UUID guard, membership guard; обновление enable, expiry, flow, limitIp, subId, totalGB |
| `create_subscription`, `extend_subscription`, `change_subscription`, bonus/promo callers | Обёртки create/update; extend от max(now, existing expiry), change от now |
| `VPNService.get_key`, `app/bot/utils/network.py`: `extract_base_url` | URL конструируется вручную; py3xui его не выдаёт |
| `app/bot/tasks/subscription_expiry.py` | Чтение expiry, уведомления; не удаляет и не отключает клиентов |
| `app/bot/routers/admin_tools/server_handler.py`, `network.ping_url` | HTTP GET health hint; HTTP 200, `ssl=False`; это не auth/provisioning validation |

Прямые imports py3xui найдены только в VPNService/ServerPoolService. Shop **не вызывает** delete, disable как отдельный endpoint, reset traffic, inbound write, Xray configuration write, server/status или token-management API. Enable=True выставляется при create/update. Автоматическое enforcement expiry/quota выполняет панель, не job магазина.

## py3xui 0.3.2 mapping

Все URL ниже относительны host, который должен включать panel web base path. Response envelope: `{success: bool, msg: string, obj: ...}`; success=False вызывает исключение.

| Class / signature | HTTP | Endpoint | Request | Parsing |
|---|---|---|---|---|
| `AsyncApi.login() -> None`, делегирует `AsyncBaseApi.login()` | POST | `/login` | JSON username/password, optional loginSecret | Cookie из response, копируется в client/inbound/database APIs |
| `AsyncInboundApi.get_list() -> list[Inbound]` | GET | `/panel/api/inbounds/list` | Cookie | obj list → Inbound, Settings/Client, Sniffing |
| `AsyncClientApi.get_by_email(email: str) -> Client | None` | GET | `/panel/api/inbounds/getClientTraffics/{email}` | Cookie; email в path | obj → Client; empty/null → None |
| `AsyncClientApi.add(inbound_id: int, clients: list[Client])` | POST | `/panel/api/inbounds/addClient` | JSON `{id, settings: JSON-string({clients:[...]})}` | Проверяет envelope, возвращает None |
| `AsyncClientApi.update(client_uuid: str, client: Client) -> None` | POST | `/panel/api/inbounds/updateClient/{client_uuid}` | JSON `{id: client.inbound_id, settings: JSON-string({clients:[one]})}` | Проверяет envelope, возвращает None |

`Client.model_dump(by_alias=True, exclude_defaults=True)` пропускает многие zero/empty defaults. HTTP transport каждый раз создаёт httpx.AsyncClient, переносит cookie `3x-ui`, TLS verification включена. Transport retries до трёх попыток применяются **также к POST** при RequestError/Timeout. Автоматического CSRF bootstrap и Bearer Authorization нет. Update логирует полный Client на INFO; shop get_key логирует полный subscription URL на DEBUG.

## Current v3.8.5 API mapping

Prefix: configured panel base path. API accepts session+CSRF, scoped Bearer, либо разрешённый mTLS (node-sync scope).

| Operation | Endpoint | Auth | Request | Response obj |
|---|---|---|---|---|
| CSRF bootstrap | GET `/csrf-token` | Public | none | Token string; session cookie |
| Login | POST `/login` | Session CSRF required | username/password/twoFactorCode | Login envelope + session cookie |
| List inbounds | GET `/panel/api/inbounds/list` | Authorized API | none | []Inbound; settings/sniffing/streamSettings JSON objects |
| Canonical client | GET `/panel/api/clients/get/{email}` | Authorized API | email | `{client: ClientRecord, inboundIds: [], externalLinks: [], usedTraffic, tunnelAllowedIPs}` |
| Traffic | GET `/panel/api/clients/traffic/{email}` | Authorized API | email | ClientTraffic or null; numeric id, UUID in uuid |
| Add | POST `/panel/api/clients/add` | Authorized API; CSRF for session | `{client: Client plus limitHwid, inboundIds: [id...]}` | null or `{nodePending:true}` in success envelope |
| Update | POST `/panel/api/clients/update/{email}` | Authorized API; CSRF for session | Flat Client plus limitHwid; optional `?inboundIds=42,43` | null or `{nodePending:true}` in success envelope |
| Subscription links | GET `/panel/api/clients/subLinks/{subId}` | Authorized API | subId | []string URLs |
| Installed versions | GET `/panel/api/server/status` | Authorized API (monitor allowed) | none | panelVersion, xray.version, other status fields |

Old getClientTraffics/addClient/updateClient routes are **not registered** in the checked v3.8.5 inbound controller/OpenAPI. API status errors may be 404 for unauthenticated requests; a 404 alone cannot prove a removed route.

## Compatibility matrix

Status относится к исходникам v3.8.5, а не к неизвестной production панели. Field-compatible rows не означают working end-to-end при несовместимом auth/routes.

| Shop operation | Shop code | py3xui method / endpoint | Current endpoint / request / response | Status | Required action |
|---|---|---|---|---|---|
| Auth | ServerPool._add_server | login, POST /login; loginSecret | /login; CSRF + twoFactorCode; cookie | CHANGED | CSRF-aware session flow или scoped token, после выбора версии/API стратегии |
| Inbound validation | validate_configured_inbound | get_list, GET /inbounds/list | Тот же route; obj []Inbound, settings objects | COMPATIBLE WITH CAVEAT | Auth fix; проверять protocol/security/enable, не только ID |
| Traffic lookup | VPN is_client_exists/get_client_data/update_client | get_by_email, GET /inbounds/getClientTraffics/email | GET /clients/traffic/email + отдельный canonical get | REMOVED | Разделить canonical identity и traffic, новые routes |
| Add | VPN create_client | add, POST /inbounds/addClient; id/settings-string | POST /clients/add; client/inboundIds; envelope | REMOVED | Новый body; строго configured ID, read-back identity |
| Update | VPN update_client | update, POST /inbounds/updateClient/UUID; id/settings-string | POST /clients/update/email; flat body, inboundIds query | REMOVED | Email route, UUID guard из canonical uuid, membership и field preservation |
| UUID identity | VPN update_client guard | Client.id из traffic id | ClientRecord.id DB integer, uuid credential string | CHANGED | Не сравнивать numeric record ID с VPN UUID |
| expiry | utils/time + VPN | expiry_time → expiryTime int | Client.expiryTime int64 milliseconds | COMPATIBLE WITH CAVEAT | Finite UTC math верна; negative delayed start/0 unlimited требуют policy |
| Quota | VPN total_gb=0 | total_gb → totalGB raw int | int64 bytes, 0 unlimited | COMPATIBLE WITH CAVEAT | Не сбрасывать finite quota непреднамеренно при edit |
| Enable | VPN create/update | enable bool | enable bool | COMPATIBLE WITH CAVEAT | Не считать flat update общим partial PATCH |
| Subscription identity | VPN sub_id=user.vpn_id | subId string | subId string, collision validation | COMPATIBLE WITH CAVEAT | Читать actual persisted subId при reconciliation |
| Device policy | devices → limit_ip | limitIp int | IP count; отдельно limitHwid | COMPATIBLE WITH CAVEAT | Не обещать точный лимит устройств через IP count |
| Flow/protocol | default Vision | flow string, no security field | Protocol-specific flow/security/password/etc | COMPATIBLE WITH CAVEAT | Ограничить/проверить VLESS transport/TLS; не универсальный multi-protocol shop |
| URL | VPN get_key | py3xui не используется | configured subURI/path/port; subLinks API | CHANGED | Читать/задавать actual subscription base; не предполагать /user/ |
| Production end-to-end | Все выше | pinned 0.3.2 | Версия панели неизвестна | NOT VERIFIED | Сначала read-only version discovery, затем отдельный non-production contract test |

## Authentication

Cookie/session auth остаётся, однако нынешний login и session-authenticated unsafe requests требуют `X-CSRF-Token` либо `_csrf` form field. py3xui 0.3.2 ни GET /csrf-token, ни header не делает: login на v3.8.5 должен завершиться CSRF rejection, даже до client endpoints. twoFactorCode заменяет предположение loginSecret; XUI_TOKEN в текущем shop — аргумент legacy loginSecret, **не API token**.

Session cookie `3x-ui`: configured base Path, HttpOnly, SameSite=Lax, Secure при HTTPS. Один сохранённый cookie не заменяет CSRF token. Прокси/TLS поведение нужно сверять в целевой установке.

Bearer tokens создаются через panel Settings/API Tokens либо POST `/panel/api/setting/apiTokens/create` авторизованным администратором: name, scope, expiresAt (epoch seconds). Random plaintext показывается один раз; хранится SHA256 hash. Backend имеет scopes `admin`, `monitor`, `node-sync`. Admin разрешает все API; monitor имеет read-only allowlist и не даёт client management; node-sync разрешает ряд mutations и inbound list, но не полный client lookup surface магазина. Поэтому нельзя считать node-sync готовым least-privilege shop token. Bearer-authenticated API обходят session CSRF. Generic OpenAPI prose про full-admin tokens менее точна, чем actual scope middleware. Token auth не внедрён.

## Client field mapping

Go Client — wire credentials; ClientRecord — canonical persisted identity; ClientTraffic — statistics. Нельзя смешивать их `id`.

| Field | Shop | py3xui 0.3.2 type/default | Backend type/default | Unit / note | Compatibility |
|---|---|---|---|---|---|
| id / uuid | User.vpn_id string UUID | id int/str/None = None; uuid отсутствует | Client.id string UUID; ClientRecord.id integer, uuid string | Credential vs DB key | CHANGED |
| email | str(tg_id) | str required | string, nonempty; canonical unique per panel | Not actual email; route identity | COMPATIBLE WITH CAVEAT |
| enable | True create/update | bool required | bool; add omission handled as true | Global client state | COMPATIBLE WITH CAVEAT |
| expiryTime | finite UTC deadline | int=0 | int64=0 | ms; 0 unlimited; negative delayed start | COMPATIBLE WITH CAVEAT |
| totalGB | normally 0 | int=0 | int64=0 | bytes, not GB | COMPATIBLE WITH CAVEAT |
| limitIp | devices | int=0 | int=0 | IP count, 0 unlimited | COMPATIBLE WITH CAVEAT |
| limitHwid | absent | absent, extra ignored | int on record/request wrapper | HWID device quota | CHANGED |
| tgId | not explicitly set | int/str/None default empty string | int64=0 | Default omitted; arbitrary string incompatible | COMPATIBLE WITH CAVEAT |
| subId | User.vpn_id | str empty default | string; add generates/checks collisions | Subscription identifier | COMPATIBLE WITH CAVEAT |
| flow | Vision default | str empty default | string; per-inbound overrides/normalization | VLESS transport/security dependent | COMPATIBLE WITH CAVEAT |
| reset | not deliberately set | int=0 | int=0 | Reset interval/policy, not traffic byte counter | COMPATIBLE WITH CAVEAT |
| comment | absent | absent, extra ignored | string empty default | Metadata loss on replacement risk | CHANGED |
| password | absent | str empty default | string (Trojan credential) | UUID alone insufficient for Trojan | COMPATIBLE WITH CAVEAT |
| security | absent | absent | string (VMess settings) | Protocol-specific | CHANGED |
| inboundId / membership | config ID + guard | inbound_id int/None; no membership list | ClientInbound association + inboundIds array | One record can belong to multiple inbounds | CHANGED |

py3xui defaults are omitted with exclude_defaults; unknown metadata is discarded. Go primitive fields have zero values, not a nullable/presence-aware PATCH contract. Client email/enable required by Pydantic; numeric null values are not supported by its int fields. Do not use null as unlimited or preservation semantics.

## expiryTime

Shop `get_current_timestamp`, `days_to_timestamp`, `add_days_to_timestamp` use UTC seconds multiplied by 1000; py3xui serializes that integer unchanged. Backend enforcement uses UnixMilli and expiry > 0 comparisons. **No finite-duration ms/seconds mismatch found.** Positive means absolute deadline; zero unlimited; negative means delayed start, converted on first traffic. Shop max(now, expiry) treats zero/negative as now, so extension does not preserve unlimited/deferred semantics. Panel updates runtime Xray users when enforcing expiry; expiryTime is panel policy, not a native Xray inbound credential expiry field. Actual enforcement interval/runtime/node status still matters.

## Traffic / totalGB

Backend copies Client.TotalGB directly into traffic.Total, compares with byte counters. No multiplication by GB in the checked add/stat code. py3xui docstring mentions GB but model/transport use raw int. 1 GiB must be 1073741824. Shop 0 means unlimited and is omitted from serialized defaults; an update can reset a previous finite limit because backend missing primitive fields become zero. Preserve existing quota/metadata explicitly in future update work. No reset endpoint is called by shop.

## Add client

Current add is one client object plus inboundIds. UUID can be generated for VMess/VLESS if omitted; shop supplies its UUID. Protocol-specific credentials differ: Trojan uses password, VMess can need security, other supported protocols have other fields. subId uniqueness is checked. Shop always defaults Vision and only validates existence of ID, not VLESS/TLS/Reality/transport or inbound enable. Backend strips inappropriate flow in some paths; this is not evidence of a universally correct link. **Unchanged shop cannot add to v3.8.5** because auth and route/body are incompatible. On an unknown legacy panel, a suitable VLESS inbound may work, but this has not been tested against production.

## Update client

Legacy py update routes by UUID but **also sends inbound ID from client.inbound_id**. UUID alone is insufficient to locate the legacy inbound. Current update routes by **email**, flat client JSON, optional inboundIds filter. Omitted credentials may be preserved by backend, but many noncredential zero defaults are applied: it is not an arbitrary partial update. Internally a single matching client entry is replaced inside the existing inbound clients array; other entries are preserved, not a full inbound configuration overwrite.

Current canonical record can span multiple inbounds. A query filter restricts inbound application but shared record fields can still affect the shared client; strict isolation requires rejecting unexpected membership/identity, not merely adding a query parameter. Backend responses can indicate nodePending: local save is not proof of remote runtime activation. Errors can occur after partial work. No blind retry is safe without reconciliation.

## Client lookup

get_by_email performs one **server GET of traffic**, not local scanning. Traffic id is numeric; UUID is a separate uuid field discarded by 0.3.2. Current shop UUID guard compares numeric id with string User.vpn_id and refuses update (verified locally using real method and real VPNService). This safely blocks modification but breaks paid renewals. New `/clients/get` returns a wrapper that old Client cannot parse directly. Canonical identity, statistics and membership need separate handling. Panel canonical email is unique; same Telegram email on different hosts refers to distinct records. A shared client across inbounds is possible, and explicit ID does not make email-only lookup inbound-specific.

## Subscription URL

Shop constructs scheme/hostname + configured port/path + User.vpn_id; default shop port 2096 and path `/user/`. Panel legacy default path is `/sub/`, while newer fresh installations can randomize subscription paths and settings allow custom subURI/HTTPS/reverse proxy. Panel `/clients/subLinks/{subId}` derives configured URLs. No URL format migration alone can infer production settings. User.vpn_id must equal persisted subId. Missing trailing slash, discarded panel base path and IPv6 formatting are additional manual URL assumptions; credentials/subscription URL must not be logged. Do not query production subscription itself for this audit: subscription fetch can update access/HWID bookkeeping.

## Multi-server

Each server has its own AsyncApi/client instances and stored session values; cookies are not shared between hosts. Credentials/token/configured inbound ID are global settings applied independently to every host. ID=42 on two hosts may have different protocol/security or be someone else's inbound; current validation only checks ID existence. Server selection uses existing least-loaded DB user count, not real panel occupancy; when all servers reach configured capacity, implementation falls back to least-loaded. No pool architecture changed here.

## Panel version discovery (do not execute here)

Read-only shell command on the panel host later: `/usr/local/x-ui/x-ui -v`. Checked main.go returns version before normal startup/DB work. Verify binary location for custom installs. Source: [main.go](https://github.com/MHSanaei/3x-ui/blob/v3.8.5/main.go).

Authorized HTTP GET `<panel-base>/panel/api/server/status` gives `obj.panelVersion` and `obj.xray.version` on checked v3.8.5; use a preexisting authorized session/monitor token and do not put secrets in shell history/logs. Older panel may expose different fields, so command is preferable if endpoint unsupported. Do not use `setting -getApiToken` as discovery: the implementation may generate/rotate a token. Never POST login/configuration solely to discover version without separate authorization.

## Xray 26.9.30

Core version is independent of panel REST routes/auth. Updating core does not restore removed panel endpoints. Flow support, VLESS/VMess/Trojan transport/TLS/Reality and runtime add-user behavior can depend on core; panel flow normalization and protocol settings also matter. This audit establishes neither a live core compatibility result nor exact features of the user-reported 26.9.30 binary. First record panel version and configured inbound protocol/security read-only; then test representative configuration in a separate non-production environment.

## py3xui future options (no selection)

| Option | Pros | Cons / maintenance risk | Migration cost |
|---|---|---|---|
| A: keep 0.3.2 | No immediate dependency churn; legacy routes implementation understood | Current upstream auth/client API incompatible; identity bug; mutation retry/metadata issues | Low only on verified legacy panel; high if implementing modern compatibility around old package |
| B: update py3xui | 0.7.0 source includes CSRF/Bearer and modern client endpoints | Still need shop changes; get picks first inboundIds entry; update lacks inboundIds filter; record id/uuid and traffic distinctions persist | Medium; contract tests and strict membership/field preservation mandatory |
| C: thin direct REST | Explicit official contract/auth/error/reconciliation control | Shop must maintain API contracts, transport safety and version matrix | Medium/high initial cost; sustained source/version verification |

0.7.0 inspection is source-only: not a verified upgrade recommendation. Its first-inbound convenience must not reintroduce Patch 1 regression. No option implemented.

## Findings by severity

| Severity | Finding | Consequence / next scope |
|---|---|---|
| HIGH | v3.8.5 requires CSRF; 0.3.2 has none | Login blocked; implement selected auth contract only after version discovery |
| HIGH | Legacy client routes absent; request/update identity changed | Add/read/renew blocked; adapt tested contracts |
| HIGH | Traffic numeric id treated as UUID | Current renew rejected; separate canonical UUID and DB ID |
| HIGH | Modern shared membership; blind library bump can fan out edits | Existing inbound/client isolation not assured; reject shared/unowned clients |
| HIGH | py3xui retries mutation POST after ambiguous transport failure | Internal repeated side effect possible despite callback state machine; disable/reconcile mutation retries in later patch |
| MEDIUM | limitIp is not exact device/HWID quota | Product device limit not guaranteed |
| MEDIUM | Shop defaults Vision/zero quota; metadata absent from model | Incorrect protocol or loss of preserved client policy on future working update |
| MEDIUM | Manual subscription base may differ from panel settings | Issued URL can fail or reference wrong endpoint |
| MEDIUM | Full key/client credential data in debug/py update info logs | Subscription credentials exposed to log readers |
| MEDIUM | Admin ping disables TLS verification | Health hint can be spoofed; provisioning TLS is still enabled |
| LOW | All-full pool falls back to least-loaded, only shop users counted | Capacity assumptions need explicit policy |

No demonstrated CRITICAL production exploit: production/version were not tested. HIGH isolation risks describe confirmed API/code behavior and prerequisites, not a claim that existing production clients have been changed.

## Patch 6B scope / recommended next checks

1. Record exact panel version and read-only configured inbound protocol/security, subscription settings and canonical membership. Preserve current explicit ID and payment states.
2. Select A/B/C separately; support only a documented panel version range. Do not blindly upgrade package.
3. Fix canonical UUID vs numeric ID distinction; separate lookup/traffic; reject mismatch, unexpected/shared inbound membership and unsupported protocol before writes.
4. Implement chosen CSRF/Bearer contract and modern add/update bodies where required; keep explicit target and delayed local server_id assignment.
5. Preserve unrelated client metadata/quota/reset/HWID/credentials; verify actual post-write state, including nodePending/runtime ambiguity, through existing reconciliation policy.
6. Remove automatic mutation retries; deterministic create/extend/change reconciliation contract before any automatic recovery.
7. Obtain/configure actual subscription base/subId; mask key/client logging. Test units, finite/zero/negative expiry and device semantics locally.
8. Contract tests against fixtures/fake server first, then a separately authorized disposable panel; never start with existing production users.

## Local tests / limitations

`tests/test_3xui_api_audit.py` uses installed py3xui/httpx MockTransport and real VPNService. Covers legacy login shape, CSRF rejection, remote traffic lookup and ignored uuid, missing client, modern wrapper incompatibility, add wire schema and units, update UUID route plus body inboundId, metadata loss, UTC day arithmetic and independent sessions. Fixtures are synthetic source-derived examples, **not production captures**. No network in these tests. Existing regression suite must also run in locked Python 3.12 environment.

Only this documentation and audit tests added. No runtime fixes, dependency updates or production requests. The checked tag and findings must be revalidated if a different panel version is chosen.
