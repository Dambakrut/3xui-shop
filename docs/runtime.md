# Local runtime baseline (Patch 5)

Supported baseline: **CPython 3.12.x**, tested on Windows with **3.12.12**.
The previous `^3.12` constraint allowed unverified Python 3.13 and newer.
The project now declares `>=3.12,<3.13`. Python 3.11 is not a supported installation
target even though many regression tests run there. The code uses `typing.Self`
(introduced in 3.11); no requirement to raise the original 3.12 minimum was found.

Use Poetry **2.1.4**, including in Docker. `poetry.lock` is now part of the project
and contains all runtime dependencies and artifact hashes. Do not regenerate it
as part of ordinary installation.

## Clean install and tests (PowerShell)

Install Python 3.12 and Poetry 2.1.4. From the repository root:

```powershell
py -3.12 -m venv .venv-runtime-test
.\.venv-runtime-test\Scripts\Activate.ps1
poetry --version
poetry check --lock
poetry install --no-root --only main --no-interaction
python -m pip check
python -m unittest discover -s tests -v
python -m alembic -c app/db/alembic.ini heads
python -m alembic -c app/db/alembic.ini history
```

Poetry respects the activated environment. Confirm `poetry env info --path` points
to `.venv-runtime-test`; an existing `.venv` may belong to a different Python.
No pip requirements file or separate pytest/lint command exists in this project.

The test suite supplies dummy credentials, blocks external connections, compiles
translations in a temporary directory, and creates temporary SQLite databases.
Runtime tests import each application module in a fresh process. Startup uses real
configuration, ORM, services, dispatcher, routers and paused schedulers. Only
Telegram/panel network responses and the HTTP listener are replaced. The Redis
client is real; its connection failure is injected at the socket boundary.
No running Redis, Telegram account, panel, payment account or production DB is
needed for these tests. Missing runtime packages cause smoke-test skips; **a clean
environment verification must finish without skips**.

## Local application prerequisites

These commands prepare an application run, unlike the isolated tests above.
They are not part of the test suite and do not deploy anything:

1. Create a local `.env` using `.env.example`. Supply a valid-format bot token,
   `BOT_WEBHOOK_SECRET`, developer/support IDs, domain, panel username/password,
   and a positive, explicit `XUI_INBOUND_ID`.
2. Stars is enabled by default. YooMoney, YooKassa, Cryptomus and Heleket are
   disabled by default; their credentials are neither required nor parsed while
   disabled. Enabled gateways require non-empty credentials; YooKassa SHOP_ID
   must be a positive integer. Missing/invalid required settings raise a config
   error instead of silently disabling the requested gateway. Credential
   authenticity is not checked offline.
3. Set `DB_DATA_DIR` to a dedicated local directory (default `app/data`). Create
   it before Alembic runs. SQLite URLs support Windows and POSIX absolute paths.
4. Supply `app/data/plans.json` using the existing plans format. Do not alter
   product pricing merely to perform a runtime check.
5. Compile translations and upgrade the selected local DB:

```powershell
python -m babel.messages.frontend compile -d app/locales -D bot
python -m alembic -c app/db/alembic.ini upgrade head
```

6. A real application run also needs a reachable Redis and configured external
   services. `REDIS_HOST` defaults to the Compose service name, so local execution
   needs an explicit host. Password-only Redis URLs and reserved characters in
   credentials are supported. No real server was contacted during verification.

`Database.initialize()` creates directories/schema for a new SQLite database;
it does **not** upgrade an existing schema. Always run Alembic before startup.
Imports alone do not read `.env`, initialize services or contact external APIs.
The actual services package is `app.bot.services`; there is no `app.services`.

## Dependency audit

There was no tracked lock before Patch 5. Existing primary package versions were
retained and pinned; direct transitive imports are now declared explicitly.

| Dependency | Previous declaration | New declaration / locked | Code use / decision |
|---|---|---|---|
| aiogram | ^3.15.0 | 3.15.0 | Bot, dispatcher, FSM, XTR, native webhook secret; tested |
| Babel | ^2.16.0 | 2.16.0 | i18n and compilation; tested |
| environs | ^11.2.1 | 11.2.1 | Config parsing; tested |
| cachetools | ^5.5.0 | 5.5.0 | Middleware caches; tested via startup |
| py3xui | ^0.3.2 | 0.3.2 | Existing AsyncApi, Client, Inbound; no adapter change |
| YooKassa | ^3.4.3 | 3.4.3 | Payment.create/find_one and models; no API call |
| SQLAlchemy[asyncio] | ^2.0.36 | 2.0.36 | ORM/AsyncSession/greenlet; SQLite verified |
| aiosqlite | ^0.20.0 | 0.20.0 | SQLite async driver; verified |
| Alembic | ^1.14.0 | 1.14.0 | Real CLI upgrade/downgrade; verified |
| redis | ^5.2.1 | 5.2.1 | RedisStorage/client initialization and failure path |
| APScheduler | ^3.11.0 | 3.11.0 | Real AsyncIOScheduler and paused registered jobs |
| aiohttp | undeclared direct import | 3.10.11 | Matches aiogram's >=3.9,<3.11 constraint |
| requests | undeclared direct import | >=2.32,<3 / 2.34.2 | YooMoney HTTP, also py3xui dependency |
| marshmallow | undeclared direct import | >=3.13,<4 / 3.26.2 | Config validators; retain v3 API used by environs 11 |
| pydantic | transitive | 2.9.2 | aiogram/py3xui shared dependency; resolver intersection |
| pydantic-settings | absent | absent | Not imported; not added |
| cryptography | absent | absent | Not imported; signatures use stdlib hashlib/hmac |
| poetry-core | unbounded | 2.1.3 | Align build backend with Poetry 2.1.4 |

The lock also records HTTPX, greenlet, tzdata/tzlocal and other transitives.
No declared primary dependency was removed as unused. `aiogram[redis]` and
`aiogram[i18n]` extras are intentionally not enabled: they impose narrower
versions than the project's separately declared packages. The actual Redis
and i18n API surfaces are tested with those packages.

## Alembic and recovery

One head: `c4e91b2a70d5`, following `b6d4e8a72c13`. The existing chain, including
`9aa6ddb8e352`, passes base-to-head and empty-transaction head-to-base downgrade.
Existing migrations were not rewritten. ORM reads all six current statuses and
the payment snapshot columns; FK definitions and unique constraints survive.
The guarded Patch 3 downgrade still refuses unresolved processing/review rows.
Do not downgrade a DB containing purchases just to test installation: Patch 4
downgrade discards validation snapshot columns.

## Boundaries left for later

- `py3xui 0.3.2` imports and exposes coroutine methods `login`,
  `inbound.get_list`, `client.get_by_email`, `client.add(inbound_id, clients)` and
  `client.update(client_uuid, client)`. Their Python signatures match shop use.
  HTTP endpoint/response compatibility with the existing panel is not tested.
- YooMoney still uses legacy Quickpay request parameters and a synchronous
  request without an explicit timeout. SDK import success does not validate a
  real checkout.
- Redis success-path I/O and Docker build/run are not verified by these tests.
  Docker is not installed in the verification environment. Compose retains its
  volumes, ports, proxy and migration-before-startup sequence. Redis is bounded
  to `7.4-bookworm` instead of `latest`; this is not a deployed upgrade.
- Default SQLAlchemy SQLite connections do not enable FK enforcement globally;
  smoke tests explicitly enable it and check integrity. No business-data policy
  was changed in this patch.
- Post-COMPLETED notification/reward crash recovery remains as in Patch 3.
- Poetry emits legacy `[tool.poetry]` metadata deprecation warnings; environs
  emits a marshmallow v4-change warning under v3. Neither blocks installation.

Patch 1–4 security semantics are retained. This work does not establish
production readiness or perform Patch 6 panel compatibility testing.
