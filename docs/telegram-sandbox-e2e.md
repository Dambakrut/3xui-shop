# Telegram sandbox E2E (manual, UI only)

## Preparation

Use a separately created test bot, never the production bot. Provide these two
variables in the **process environment**, not as command-line arguments:

- `TELEGRAM_SANDBOX_BOT_TOKEN`: the separate test bot token.
- `TELEGRAM_E2E_ALLOWED_USER_ID`: one positive Telegram user ID.

There is no fallback to `BOT_TOKEN`. The script does not load `.env`, production
config, DB paths, Redis credentials, payment secrets or XUI credentials. Do not
paste tokens into reports or commit them. Creating the test bot is a manual step.

After explicit live-run approval, launch from the repository root:

```powershell
.venv-runtime-test\Scripts\python.exe scripts\telegram_sandbox_e2e.py
```

This command performs real Telegram requests. It is not a dry-run command.
It must not be run automatically during tests or startup.

## Isolation

The script owns a fresh OS temporary directory. Alembic upgrades its SQLite file
to `d7a2f6c890e1`; the migration environment receives only this generated URL via
a scoped config substitution. No production config is evaluated. Translation
catalogs are compiled into the same temporary directory. FSM uses MemoryStorage.
Production entrypoint, startup/shutdown hooks, commands setup, webhook handlers,
schedulers, Redis, admin/trial/payment-success routers and developer notifications
are not installed.

Dispatcher built-in FSM registration is disabled. A private-chat/user gate runs
before the sandbox FSM middleware and DB middleware. Other users, channel/inline
updates, invoice/successful-payment messages and pre-checkout events are silently
dropped. No unauthorized response or content logging occurs.

Only `/start`, main-menu, subscription, buy, device/duration selection, fake payment
selection and back handlers are registered. Profile/support buttons inherited
from the main-menu layout are outside this harness and have no registered route.
The sandbox catalogue contains one device, 30 days, 50 XTR as **test data**, not a
production pricing change. The payment method is explicitly marked SANDBOX.

The fake gateway inherits the real durable reservation implementation. Its action
is `https://example.test/sandbox/<flow>` and cannot be paid. No Stars invoice, other
provider SDK or HTTP provider call is made. Fake XUI supplies only an available
server placeholder; sync/provisioning methods raise a fixed sandbox exception.

## Telegram safety

Before polling, the script calls `getWebhookInfo`. A nonempty URL stops the script:
it never deletes or changes the webhook. Then `getMe` prints only bot ID, username
and first name so the operator can recognize the intended test bot.

The sequential `getUpdates` loop is explicit; aiogram's automatic polling backoff
and parallel update tasks are not used. Polling conflict stops immediately. Other
unexpected errors stop with exception type and redacted details, without retries
or auth fallback. Ctrl+C stops polling. Avoid a second process for the same token;
the webhook check cannot prove that no other polling process exists.

Real transport permits only `getWebhookInfo`, `getMe`, `getUpdates`, `sendMessage`,
`editMessageText`, `deleteMessage` and `answerCallbackQuery`. Chat methods can target
only the allowlisted private chat. Payment invoice/webhook/command mutation API
methods are rejected before network I/O.

### Proxy-aware sandbox networking

Only `TelegramOnlySession` constructs its aiohttp client with `trust_env=True`.
It respects the proxy settings available to aiohttp/urllib in the launching
environment, including Windows system proxy discovery where supported. No proxy
address, credentials or port are stored in source. If proxy discovery returns no
applicable proxy, aiohttp uses its ordinary direct connection. TLS verification
remains enabled. The production aiogram session/runtime is unchanged.

Network errors stop without retry and print only `TelegramNetworkError`, a fixed
timeout/connector/SSL/DNS/unknown category and host `api.telegram.org`. Bot API
paths, tokens, proxy credentials, raw headers and exception messages are excluded.
This coding change does not run a live network check or polling.

## Manual scenarios

1. Send `/start` from the allowlisted private test user.
2. Open subscription, buy, plan, duration and SANDBOX payment method.
3. Expect a payment button pointing to `example.test`; do not try real payment.
4. Back to payment-method selection and choose it again: same flow/action, one
   transaction and one fake invoice call. Redelivery of the same callback has
   the same semantics. The original method button is replaced by the payment UI,
   so ordinary repeated tapping is exercised through back navigation.
5. An explicit new buy action creates a new flow and may create another order.
6. Stop with Ctrl+C and inspect the console summary.

No successful payment simulation, subscription fetch or provisioning is part of
this stage. Live runs are manual and require separate approval; coding/tests do
not automatically launch polling.

## Summary, privacy and cleanup

On exit, a summary contains transaction count, redacted flow prefixes, statuses,
fake invoice calls, reuse count, payment host `example.test`, Telegram request
attempts and the sandbox-local path. No payment URL, UUID, payload, charge ID,
user ID, Update dump or token is printed. Library/handler logging is suppressed
for this standalone process; failures use fixed safe messages or exception types.

The summary is printed before closing storage/session/engine. The owned temporary
directory is removed automatically on normal exit/Ctrl+C. Forced process kill can
leave temporary test files; they are never production DB files. No keep-DB option
or production cleanup command is provided. Do not publish temporary DB contents.

### Checkout event telemetry

A sandbox callback middleware observes selected runtime handlers; it does not
alter their filters, callback data, FSM transitions or durable purchase semantics.
It reads sandbox DB/FSM state before and after handling. In-memory events contain
only sequence, fixed event type, redacted flow prefixes, created/reused/unchanged
outcome, invoice call delta, transaction count, reuse count and transition flags.
No raw Update, message text, callback data, full flow ID, URL or token is retained
in the timeline. A non-exported SHA-256 fingerprint correlates a back transition
to a later reuse without retaining the full purchase flow identity in telemetry.

The shutdown JSON includes `event_timeline`, `blocked_update_count` and assertions;
a short `seq | event | flow | created | reused | invoice_delta` table follows.
Each payment selection records active flow before/after and whether the observed
call created a reservation or actually returned an existing payment action.

- **duplicate reuse observed:** at least one confirmed reuse with created=false
  and invoice_delta=0. It is FAIL when no reuse was observed, even if two different
  intentional checkouts worked correctly.
- **back reuse observed:** back/reopen retained the same flow, followed by an
  actual payment reuse for that flow with zero invoice calls.
- **intentional new checkout observed:** explicit buy changed the flow and the
  subsequent payment selection created one new transaction with invoice_delta=1.
  Merely opening a new checkout is not sufficient.

Counters now include blocked updates; their content is never recorded. Main-menu
navigation may clear FSM under existing runtime semantics; the timeline exposes
this, and an explicit later buy is a new checkout. Use the payment action's
change-payment-method back button to test retention of the active checkout.

The preceding live trial confirmed Telegram/UI access, isolation and two distinct
flows/orders/invoices. Its reuse counter was zero, so it did not prove duplicate
or back reuse; this is not evidence of a runtime regression. This telemetry change
did not retrospectively reclassify that trial.

## Live evidence: composite verification

**SANDBOX E2E VERIFIED** by the combined evidence of two separately approved
manual live runs, not by declaring either run a complete standalone PASS.

| Evidence | Run 1 | Run 2 |
| --- | --- | --- |
| Telegram/UI access | Confirmed | Confirmed |
| First checkout | Confirmed | created=true, reused=false, invoice_delta=1 |
| Duplicate same-flow reuse | Not proved (reuse count 0) | reused=true, created=false, invoice_delta=0 |
| Back/navigation reuse | Not proved | Flow retained; subsequent payment reused, invoice_delta=0 |
| Intentional new checkout | 2 distinct flows, 2 transactions, 2 fake invoices | Not performed/observed in this run |
| Production isolation | PASS | PASS |

Run 2 ended with one pending transaction, one fake invoice, one reuse and zero
blocked updates. Its duplicate and back assertions were PASS; its intentional-new-
checkout assertion remained FAIL/not observed. Run 2 is **not** reclassified as a
full standalone PASS. Run 1 supplies the separate intentional-new-checkout evidence.

Neither run invoked real payment APIs, provisioning or production XUI, used or
mutated production DB/Redis, or changed any webhook. Both used temporary SQLite,
MemoryStorage, fake payment and fake XUI. Temporary databases/directories were
removed after safe shutdown summaries. No token, user ID, full flow identity,
payment URL, proxy address or raw Telegram content is included in this evidence.

This verifies the UI-only sandbox boundary. It does not verify real payments,
successful-payment delivery, provisioning or production Telegram deployment.

## Local tests

`tests/test_telegram_sandbox.py` uses recording Telegram transport and blocks
external socket connections. It checks credential requirements, early allowlist,
migration head, real UI routing, durable fake payment reuse, new checkout,
payment-update rejection, webhook/conflict stop, outbound method/recipient guard
and redacted summaries. Full repository regressions remain required before review.

Verification including proxy support and event telemetry: locked Python 3.12.12,
**278 tests passed, zero skips** (20 sandbox tests). Poetry lock, pip dependency
and whitespace checks passed. This observability coding stage performed zero
Telegram/payment/3x-ui requests. Separately approved live runs provide the composite
evidence above; local test fixtures contain only fabricated user/bot identities
and placeholder `.test`/`.invalid` URLs, never real credentials.
