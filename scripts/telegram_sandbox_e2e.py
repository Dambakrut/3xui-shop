"""Manual UI-only Telegram sandbox. Never call the production entrypoint."""
import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sys
import tempfile
from types import SimpleNamespace as NS
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aiogram import BaseMiddleware, Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiohttp import ClientSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramConflictError, TelegramNetworkError
from aiogram.fsm.middleware import FSMContextMiddleware
from aiogram.fsm.storage.memory import DisabledEventIsolation, MemoryStorage
from aiogram.utils.i18n import I18n, SimpleI18nMiddleware
from alembic import command
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bot.middlewares.database import DBSessionMiddleware
from app.bot.payment_gateways._gateway import PaymentGateway
from app.bot.payment_gateways.gateway_factory import GatewayFactory
from app.bot.routers.main_menu import handler as main_menu
from app.bot.routers.subscription import payment_handler, subscription_handler
from app.bot.services.notification import NotificationService
from app.bot.models import CheckoutData, Plan
from app.bot.filters import IsAdmin, IsDev
from app.bot.utils.constants import Currency
from app.bot.utils.navigation import NavSubscription
from app.config import DEFAULT_LOCALES_DIR
from app.db.models import Transaction


class SandboxError(RuntimeError):
    """Fixed safe messages only; never wrap API exception text."""


def network_diagnostic(error):
    """Classify locally; never return exception messages, URLs or headers."""
    hints = []
    seen = set()
    current = error
    while current is not None and id(current) not in seen and len(seen) < 8:
        seen.add(id(current))
        hints.append(type(current).__name__.lower())
        # aiogram can wrap aiohttp's type in a message instead of chaining it.
        hints.append(str(getattr(current, "message", "")).lower())
        current = current.__cause__ or current.__context__
    text = " ".join(hints)
    category = next((label for label, markers in (
        ("SSL", ("ssl", "certificate")),
        ("DNS", ("dns", "name resolution", "getaddrinfo")),
        ("timeout", ("timeout", "timed out")),
        ("connector", ("connector", "connection", "connect error")),
    ) if any(marker in text for marker in markers)), "unknown")
    return f"TelegramNetworkError; category={category}; host=api.telegram.org; details redacted; no retry"


def credentials(env=None):
    env = os.environ if env is None else env
    token = env.get("TELEGRAM_SANDBOX_BOT_TOKEN", "")
    raw_user = env.get("TELEGRAM_E2E_ALLOWED_USER_ID", "")
    if not token or not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{30,}", token):
        raise SandboxError("A separate valid TELEGRAM_SANDBOX_BOT_TOKEN is required")
    if not re.fullmatch(r"[1-9][0-9]*", raw_user):
        raise SandboxError("A positive TELEGRAM_E2E_ALLOWED_USER_ID is required")
    # Deliberately no BOT_TOKEN fallback, dotenv loading, or load_config().
    return token, int(raw_user)


class TelegramOnlySession(AiohttpSession):
    """Restrict the real transport to UI methods and the allowlisted chat."""
    METHODS = {"getMe", "getWebhookInfo", "getUpdates", "sendMessage",
               "editMessageText", "deleteMessage", "answerCallbackQuery"}

    def __init__(self, allowed_user):
        super().__init__()
        self.allowed_user = allowed_user
        self.requests = 0

    async def create_session(self):
        # The locked aiogram AiohttpSession does not expose trust_env. Keep its
        # connector/TLS/lifetime contract, changing only the sandbox HTTP client.
        if self._should_reset_connector:
            await self.close()
        if self._session is None or self._session.closed:
            self._session = ClientSession(
                connector=self._connector_type(**self._connector_init),
                trust_env=True,
            )
            self._should_reset_connector = False
        return self._session

    async def make_request(self, bot, method, timeout=None):
        if method.__api_method__ not in self.METHODS:
            raise SandboxError("Telegram method blocked by sandbox")
        chat_id = getattr(method, "chat_id", None)
        if chat_id is not None and chat_id != self.allowed_user:
            raise SandboxError("Non-allowlisted Telegram recipient blocked")
        self.requests += 1
        return await super().make_request(bot, method, timeout)


class SandboxGate(BaseMiddleware):
    def __init__(self, allowed_user):
        self.allowed_user = allowed_user
        self.accepted = 0
        self.blocked = 0

    async def __call__(self, handler, event, data):
        message = event.message
        callback = event.callback_query
        source = message or callback
        user = getattr(source, "from_user", None)
        chat = message.chat if message else getattr(getattr(callback, "message", None), "chat", None)
        # Only private UI updates; payment, pre-checkout, edited/channel/inline
        # events never enter FSM, DB or business logic.
        if (source is None or user is None or user.is_bot or user.id != self.allowed_user
                or chat is None or chat.type != "private" or chat.id != self.allowed_user
                or (message and (message.successful_payment or message.invoice))):
            self.blocked += 1
            return None
        self.accepted += 1
        return await handler(event, data)


class FakePayment(PaymentGateway):
    provider = "sandbox"
    callback = NavSubscription.PAY_TELEGRAM_STARS  # Existing UI text/price contract only.
    currency = Currency.XTR
    name = "SANDBOX — no payment"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.invoice_calls = 0
        self.reuse_count = 0

    async def _create_invoice(self, data, transaction):
        self.invoice_calls += 1
        return (f"https://example.test/sandbox/{transaction.purchase_flow_id}",
                transaction.payment_id, None)

    async def create_payment(self, data, purchase_flow_id=None):
        before = self.invoice_calls
        result = await super().create_payment(data, purchase_flow_id)
        if before == self.invoice_calls:
            self.reuse_count += 1
        return result

    async def handle_payment_succeeded(self, payment_id):
        raise SandboxError("Successful payment is disabled in sandbox")

    async def handle_payment_canceled(self, payment_id):
        raise SandboxError("Payment processing is disabled in sandbox")


class FakeXUI:
    calls = 0

    async def get_available_server(self):
        return NS(id=1, name="Sandbox placeholder")

    async def denied(self, *args, **kwargs):
        raise SandboxError("Panel/provisioning operations are disabled in sandbox")

    sync_servers = create_subscription = extend_subscription = change_subscription = denied
    get_client_data = is_client_exists = get_limit_ip = denied


class SandboxNotification(NotificationService):
    async def notify_developer(self, *args, **kwargs):
        raise SandboxError("Developer notifications are disabled in sandbox")


def redact_flow(value):
    return value[:6] + "…" if value else None


class CheckoutTelemetry(BaseMiddleware):
    """Observe real handlers without storing Telegram content or flow identities."""
    def __init__(self, sessions, gateway, gate):
        self.sessions, self.gateway, self.gate = sessions, gateway, gate
        self.events = []
        # Nonreversible correlation only; never exported as an event field.
        self.back_fingerprint = None

    async def snapshot(self, flow):
        async with self.sessions() as session:
            rows = list((await session.scalars(select(Transaction).order_by(Transaction.id))).all())
        return rows, next((r for r in rows if r.purchase_flow_id == flow), None)

    async def __call__(self, handler, event, data):
        callback = data["handler"].callback
        relevant = {subscription_handler.callback_subscription_process,
                    subscription_handler.callback_devices_selected,
                    subscription_handler.callback_duration_selected,
                    payment_handler.callback_payment_method_selected,
                    main_menu.callback_main_menu, subscription_handler.callback_subscription}
        if callback not in relevant:
            return await handler(event, data)
        state = data["state"]
        before_context = await state.get_data()
        before_flow = before_context.get("checkout_flow_id")
        selected = data.get("callback_data")
        payment = isinstance(selected, CheckoutData)
        target_flow = selected.flow_id if payment else before_flow
        before_rows, before_row = await self.snapshot(target_flow)
        invoices, reuses = self.gateway.invoice_calls, self.gateway.reuse_count
        result = await handler(event, data)
        after_flow = (await state.get_data()).get("checkout_flow_id")
        target_flow = target_flow if payment else after_flow or before_flow
        after_rows, after_row = await self.snapshot(target_flow)
        invoice_delta = self.gateway.invoice_calls - invoices
        reused = payment and self.gateway.reuse_count > reuses and before_row is not None
        created = payment and before_row is None and after_row is not None
        back_reuse = False
        if callback is subscription_handler.callback_subscription_process:
            # Main-menu navigation can clear FSM. A previous DB flow still
            # supplies a local comparison for the new explicit buy action.
            old_flow = before_flow or (before_rows[-1].purchase_flow_id if before_rows else None)
            event_type = "intentional_new_checkout" if old_flow else "checkout_started"
            before_flow = old_flow
            self.back_fingerprint = None
        elif payment:
            event_type = "payment_reused" if reused else "payment_method_selected"
            fingerprint = hashlib.sha256(target_flow.encode()).hexdigest()
            back_reuse = bool(reused and self.back_fingerprint == fingerprint)
        elif (callback in (subscription_handler.callback_devices_selected,
                           subscription_handler.callback_duration_selected)
              and before_context.get("checkout_subscription") is not None):
            event_type = "back_navigation"
            if before_flow and before_flow == after_flow:
                self.back_fingerprint = hashlib.sha256(before_flow.encode()).hexdigest()
        elif callback in (main_menu.callback_main_menu, subscription_handler.callback_subscription):
            event_type = "back_navigation"
            if before_flow != after_flow:
                self.back_fingerprint = None
        else:
            event_type = "payment_methods_opened" if callback is subscription_handler.callback_duration_selected else "checkout_navigation"
        self.events.append({"seq": len(self.events) + 1, "event": event_type,
            "flow": redact_flow(target_flow),
            "active_flow_before": redact_flow(before_flow), "active_flow_after": redact_flow(after_flow),
            "flow_unchanged": bool(before_flow and before_flow == after_flow),
            "flows_differ": bool(before_flow and after_flow and before_flow != after_flow),
            "outcome": "created" if created else "reused" if reused else "unchanged",
            "created": bool(created), "reused": bool(reused), "invoice_delta": invoice_delta,
            "transaction_count": len(after_rows), "reuse_count": self.gateway.reuse_count,
            "back_reuse": back_reuse})
        return result

    def assertions(self):
        intentional_created = any(
            start["event"] == "intentional_new_checkout" and start["flows_differ"]
            and any(e["seq"] > start["seq"] and e["flow"] == start["active_flow_after"]
                    and e["created"] and e["invoice_delta"] == 1 for e in self.events)
            for start in self.events)
        return {
            "duplicate_reuse_observed": "PASS" if any(e["reused"] and not e["created"] and e["invoice_delta"] == 0 for e in self.events) else "FAIL",
            "back_reuse_observed": "PASS" if any(e["back_reuse"] and e["invoice_delta"] == 0 for e in self.events) else "FAIL",
            "intentional_new_checkout_observed": "PASS" if intentional_created else "FAIL",
        }


def print_timeline(report):
    print("## Event timeline\nseq | event | flow | created | reused | invoice_delta")
    for event in report["event_timeline"]:
        print(f"{event['seq']} | {event['event']} | {event['flow'] or '-'} | "
              f"{event['created']} | {event['reused']} | {event['invoice_delta']}")
    print("## Counters")
    print(json.dumps({key: report[key] for key in
          ("transaction_count", "fake_invoice_calls", "duplicate_reuse_count", "blocked_update_count")}))
    print("## Assertions")
    print(json.dumps(report["assertions"]))


def migrate_sandbox(directory):
    """Fresh owned directory only. No production config is evaluated by env.py."""
    path = Path(directory) / "telegram-sandbox.sqlite3"
    if path.exists():
        raise SandboxError("Sandbox database must be fresh")
    url = f"sqlite+aiosqlite:///{path.as_posix()}"
    config = AlembicConfig()
    config.set_main_option("script_location", str(ROOT / "app/db/migration"))
    if ScriptDirectory.from_config(config).get_heads() != ["d7a2f6c890e1"]:
        raise SandboxError("Unexpected migration head; review sandbox contract")
    # env.py calls load_config solely for the database URL. Substitute before
    # loading env.py, scoped to this isolated script/process; no .env is read.
    with patch("app.config.load_config", return_value=NS(database=NS(url=lambda: url))):
        command.upgrade(config, "head")
    return path, url


def translations(directory):
    from babel.messages.mofile import write_mo
    from babel.messages.pofile import read_po
    target = Path(directory) / "locales"
    for locale in ("en", "ru"):
        output = target / locale / "LC_MESSAGES/bot.mo"
        output.parent.mkdir(parents=True)
        with (DEFAULT_LOCALES_DIR / locale / "LC_MESSAGES/bot.po").open("rb") as source:
            catalog = read_po(source)
        with output.open("wb") as stream:
            write_mo(stream, catalog)
    return I18n(path=target, default_locale="en", domain="bot")


def build_dispatcher(bot, allowed_user, sessions, i18n):
    storage = MemoryStorage()
    # Disable built-in FSM registration so the gate precedes even FSM reads.
    dispatcher = Dispatcher(storage=storage, disable_fsm=True)
    gate = SandboxGate(allowed_user)
    dispatcher.update.outer_middleware(gate)
    dispatcher.update.outer_middleware(FSMContextMiddleware(storage, DisabledEventIsolation()))
    dispatcher.update.outer_middleware(SimpleI18nMiddleware(i18n))
    dispatcher.update.outer_middleware(DBSessionMiddleware(sessions))
    IsDev.set_developer(0)
    IsAdmin.set_admins([])
    config = NS(bot=NS(DEV_ID=0, ADMINS=[], SUPPORT_ID=0),
                shop=NS(CURRENCY="XTR", REFERRER_REWARD_ENABLED=False))
    xui = FakeXUI()
    plan = Plan(devices=1, prices={"XTR": {30: 50}})
    async def unavailable(*args, **kwargs):
        return False
    services = NS(server_pool=xui, vpn=xui,
        plan=NS(get_plan=lambda n: plan if n == 1 else None,
                get_all_plans=lambda: [plan], get_durations=lambda: [30]),
        notification=SandboxNotification(config, bot),
        subscription=NS(is_trial_available=unavailable),
        referral=NS(is_referred_trial_available=unavailable))
    gateway = FakePayment(None, config, sessions, storage, bot, i18n, services)
    gateway.telemetry = CheckoutTelemetry(sessions, gateway, gate)
    factory = GatewayFactory()
    factory.register_gateway(gateway)
    dispatcher.workflow_data.update(config=config, services=services, gateway_factory=factory)
    allowed = {main_menu.command_main_menu, main_menu.callback_main_menu,
        subscription_handler.callback_subscription, subscription_handler.callback_subscription_process,
        subscription_handler.callback_devices_selected, subscription_handler.callback_duration_selected,
        payment_handler.callback_payment_method_selected, payment_handler.callback_legacy_payment_selection}
    # Copy only selected registrations into new routers. No ownership mutation
    # of application routers; successful payment, trials and admin omitted.
    for source in (main_menu.router, subscription_handler.router, payment_handler.router):
        router = Router()
        router.callback_query.middleware(gateway.telemetry)
        for kind in ("message", "callback_query"):
            for handler in getattr(source, kind).handlers:
                if handler.callback in allowed:
                    getattr(router, kind).register(handler.callback, *(f.callback for f in handler.filters))
        dispatcher.include_router(router)
    return dispatcher, gateway, gate


async def poll_once_loop(bot, dispatcher):
    """No aiogram polling backoff, parallel tasks, session/login fallback."""
    webhook = await bot.get_webhook_info()
    if webhook.url:
        raise SandboxError("Webhook exists; stopped without changing it")
    identity = await bot.get_me()
    print(json.dumps({"bot_id": identity.id, "username": identity.username,
                      "first_name": identity.first_name}))
    offset = None
    try:
        while True:
            updates = await bot.get_updates(offset=offset, timeout=20,
                                           allowed_updates=["message", "callback_query"])
            for update in updates:
                await dispatcher.feed_update(bot, update)
                offset = update.update_id + 1
    except TelegramConflictError:
        raise SandboxError("Polling conflict: another instance is running; stopped without retry") from None


async def summary(sessions, gateway, transport, path):
    async with sessions() as session:
        rows = list((await session.scalars(select(Transaction))).all())
    return {"transaction_count": len(rows),
        "transactions": [{"flow": (r.purchase_flow_id or "")[:6] + "…",
                          "status": r.status.value, "payment_host": "example.test" if r.payment_url else None}
                         for r in rows],
        "fake_invoice_calls": gateway.invoice_calls, "duplicate_reuse_count": gateway.reuse_count,
        "blocked_update_count": gateway.telemetry.gate.blocked,
        "event_timeline": gateway.telemetry.events,
        "assertions": gateway.telemetry.assertions(),
        "telegram_request_attempts": transport.requests, "sandbox_db": str(path)}


async def run():
    token, allowed_user = credentials()
    # Suppress library/handler logs that might include identity or exception
    # payloads. Only this script's fixed messages and redacted summaries print.
    logging.disable(logging.CRITICAL)
    with tempfile.TemporaryDirectory(prefix="telegram-sandbox-") as directory:
        path, url = await asyncio.to_thread(migrate_sandbox, directory)
        engine = create_async_engine(url)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        transport = TelegramOnlySession(allowed_user)
        dispatcher = None
        try:
            bot = Bot(token, session=transport, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
            dispatcher, gateway, gate = build_dispatcher(bot, allowed_user, sessions, translations(directory))
            print("TELEGRAM SANDBOX E2E MODE\ntemporary DB; MemoryStorage; fake payment; fake XUI; allowlisted user configured")
            await poll_once_loop(bot, dispatcher)
        finally:
            try:
                if dispatcher is not None:
                    report = await summary(sessions, gateway, transport, path)
                    print(json.dumps(report))
                    print_timeline(report)
            finally:
                if dispatcher is not None:
                    await dispatcher.storage.close()
                await transport.close()
                await engine.dispose()
                print("Sandbox-only temporary data scheduled for removal on exit")


def main():
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("Sandbox stopped by operator")
    except SandboxError as error:
        print(str(error))
        return 1
    except TelegramNetworkError as error:
        print("Sandbox stopped: " + network_diagnostic(error))
        return 1
    except Exception as error:
        print(f"Sandbox stopped: {type(error).__name__}; details redacted; no retry")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
