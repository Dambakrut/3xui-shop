# Project Instructions

## Project overview

This repository implements a Telegram VPN shop. The application uses aiogram 3.x,
async SQLAlchemy, Alembic migrations, SQLite by default, and Redis for the normal
runtime FSM. Current payment gateways are Telegram Stars, Cryptomus, Heleket,
YooKassa, and YooMoney. Panel access is through the project's async 3x-ui adapter.

## Architecture

- `app/__main__.py` is the production webhook entrypoint. It initializes the DB,
  Redis FSM, services, server sync, payment gateways, webhook, and schedulers.
- `app/bot/routers/` contains Telegram handlers and checkout/navigation flow;
  `app/bot/payment_gateways/` implements provider and payment-state handling.
- `app/bot/services/` contains VPN and server-pool business logic.
- `app/db/` contains async DB setup, ORM models, and Alembic migrations.
- `app/integrations/xui/` contains the independent modern 3x-ui HTTP adapter.
- `scripts/` contains manually invoked smoke tools. In particular,
  `telegram_sandbox_e2e.py` is a separate guarded sandbox entrypoint.
- `tests/fixtures/` contains synthetic API fixtures; `docs/` contains source
  audits and operational evidence.

Checkout reserves a durable transaction before creating a provider invoice. A
confirmed payment is claimed for processing, then provisioning either completes
or moves to manual review. Referral rewards follow successful completion; they
must not be granted again for duplicate payment callbacks.

## Development environment

Use CPython 3.12.x (`>=3.12,<3.13`) and Poetry 2.1.4 with the checked-in
`poetry.lock`. Do not regenerate the lockfile during routine setup. For Windows
PowerShell, the documented clean environment setup is:

```powershell
py -3.12 -m venv .venv-runtime-test
.\.venv-runtime-test\Scripts\Activate.ps1
poetry --version
poetry env info --path
poetry install --no-root --only main --no-interaction
```

Run checks from that activated project environment (or prefix Python commands
with `poetry run` to select Poetry's environment):

```powershell
poetry check --lock
python -m pip check
python -m unittest discover -s tests -v
git diff --check
```

For migration graph inspection, use:

```powershell
python -m alembic -c app/db/alembic.ini heads
python -m alembic -c app/db/alembic.ini history
```

Before any `upgrade` or `downgrade`, verify the resolved database URL targets a
disposable local/test database. Alembic loads application configuration; never
assume a developer's `.env` points to a safe database. Tests use isolated local
data and must not use production DB or Redis.

## Important invariants

1. Runtime accepts only the explicit 3x-ui panel versions `3.8.5` and `3.9.0`.
   Keep the allowlist explicit; fail closed on every other version.
2. Runtime panel operations use `XUIAdapter`. Do not reintroduce `py3xui` imports
   or legacy panel calls into application runtime.
3. `XUI_INBOUND_ID` is required. Never select the first inbound as a fallback.
4. Shop provisioning supports VLESS + Reality + XHTTP with `flow=""` when the
   inbound capability checks pass. Do not assume TCP or Vision is required.
   Unsupported or mixed shared-inbound capabilities fail closed.
5. Canonical client UUID, subscription ID (`subId`), and numeric traffic-record
   ID are separate identities. Never use a traffic ID as a client UUID.
6. 3x-ui client expiry is epoch milliseconds; quota (`totalGB`) is represented
   in bytes by the API contract. Preserve units at every boundary.
7. Client updates are full-model writes. Preserve allowlisted existing fields,
   identity, and all memberships; do not echo server-only IDs/timestamps or
   blindly round-trip unknown raw fields. Respect stale-snapshot checks.
8. Never automatically retry a panel mutation after an ambiguous outcome. Use
   the adapter's read-after-write reconciliation and manual-review path.
9. For add/update, `node_pending=False` is established only by a valid successful
   mutation response with the source-verified no-pending contract (including
   `success=true, obj=null`). `node_pending=True` or `None` is not confirmed node
   activation and requires `REVIEW_REQUIRED`. Persistence is not node activation.
10. Checkout uses a durable unique `purchase_flow_id`; reserve it atomically and
    commit the reservation before provider invoice I/O. A duplicate flow reuses
    its existing payment action only when that action is durably saved. An
    in-flight or review-required reservation must not start another provider call;
    do not reopen completed transactions.
11. Duplicate payment callbacks must not repeat provisioning or referral rewards.
    Ambiguous provider or provisioning outcomes must not trigger blind retries.
12. Never log or send raw Telegram Updates, payment/provider payloads, callback
    data, credentials, tokens, or sensitive payment identifiers to diagnostics.

## Production and sandbox safety

- Production Telegram, payment, 3x-ui, database, Redis, migration, and deployment
  actions require separate explicit authorization. Tests and local mocks are not
  authorization to contact production.
- Do not read or change `.env` unless necessary and authorized. Never print or
  copy secrets into source, tests, docs, command output, or reports.
- Keep sandbox resources isolated from production. Do not use the normal
  `app/__main__.py` webhook startup for sandbox testing: it changes webhook
  configuration, starts schedulers, and sends developer notifications.
- Use only the sandbox entrypoint and its separate token, allowlisted user,
  temporary SQLite, MemoryStorage, fake payment, and fake XUI when an authorized
  Telegram sandbox run is requested. Do not simulate successful payment or
  provisioning unless separately authorized.
- Do not push, merge, deploy, or run production migrations without explicit
  authorization.

## Development workflow

- Inspect the affected handlers, service, model, adapter, and tests before editing.
- Keep changes narrowly scoped. Do not bundle unrelated refactors or dependency
  updates into a bug fix.
- Add regression coverage for the behavior being changed; retain payment,
  transaction-state, privacy, and adapter safety guarantees.
- Do not guess an external API contract. Check the pinned/source-verified panel
  contract and update its audit/tests when behavior changes.
- Update the relevant docs when a contract or operational procedure changes.
- Before finishing, review the complete diff, run applicable checks, and scan
  changed files for secret or personal-data exposure.
- Label evidence accurately: mock/local tests do not prove live API compatibility.

## Verified project status

The 3x-ui 3.9.0 synthetic-client lifecycle was live-verified and cleaned up.
Telegram sandbox UI E2E is verified from the combined evidence of two runs.
Durable checkout idempotency is implemented. Payment E2E and deployment/release
validation remain future work; this status is not a production-readiness claim
and does not authorize starting those stages.

## Reference docs

- [3x-ui adapter contract](docs/3xui-adapter.md)
- [3x-ui 3.9.0 source audit](docs/3xui-390-audit.md)
- [3x-ui lifecycle smoke evidence](docs/3xui-live-write-smoke.md)
- [Telegram E2E findings](docs/telegram-e2e-audit.md)
- [Telegram sandbox procedure and evidence](docs/telegram-sandbox-e2e.md)

Read the relevant reference for the task at hand; do not load all docs into every
session. See Anthropic's [Claude Code memory guidance](https://code.claude.com/docs/en/memory)
and [best practices](https://code.claude.com/docs/en/best-practices) for the
project-level instruction-file conventions.
