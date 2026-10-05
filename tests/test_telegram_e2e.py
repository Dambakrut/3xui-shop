"""Offline dispatch → Stars order → DB claim → real VPNService integration.

Telegram transport and the panel boundary are fakes; handlers, middleware,
FSM, Stars validation, ORM and subscription writes use imported runtime code.
No environment configuration or production credentials are read.
"""
import asyncio
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from aiogram import Bot, Dispatcher, Router
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, Update
from aiogram.utils.i18n import I18n
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bot.filters import IsAdmin, IsDev
from app.bot.middlewares.database import DBSessionMiddleware
from app.bot.models import CheckoutData, Plan, SubscriptionData
from app.bot.payment_gateways.telegram_stars import TelegramStars
from app.bot.utils.payment_security import CheckoutUnavailable, InvalidPayment
from app.bot.routers.main_menu import handler as main
from app.bot.routers.subscription import payment_handler as payment
from app.bot.routers.subscription import subscription_handler as subscription
from app.bot.routers.admin_tools import admin_tools_handler as admin
from app.bot.services.notification import NotificationService
from app.bot.services.server_pool import Connection, ServerPoolService
from app.bot.services.vpn import VPNService
from app.bot.utils.constants import Currency, TransactionStatus
from app.bot.utils.navigation import NavSubscription as Nav
from app.config import DEFAULT_LOCALES_DIR
from app.db.models import Base, Server, Transaction, User
from app.integrations.xui import XUIClient, XUIClientTraffic, XUIInboundSummary, XUINotFoundError, XUIWriteResult


class RecordingTelegram(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):
        raise AssertionError("Telegram download forbidden")
        yield b""  # async-generator interface

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        name = method.__api_method__
        if name == "createInvoiceLink":
            return "https://invoice.example.test/local-order"
        if name in ("sendMessage", "editMessageText"):
            return Message(message_id=100, date=0, chat={"id": 700, "type": "private"},
                           text=method.text, reply_markup=method.reply_markup).as_(bot)
        if name in ("deleteMessage", "answerCallbackQuery", "answerPreCheckoutQuery"):
            return True
        raise AssertionError(f"Unexpected Telegram method: {name}")


class TelegramOfflineE2E(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self.temp.name).as_posix()}/test.db")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.sender = RecordingTelegram()
        self.bot = Bot("246802468:" + "a" * 35, session=self.sender)
        self.storage = MemoryStorage()
        # Compile shipped translations into the temporary test directory only.
        from babel.messages.pofile import read_po
        from babel.messages.mofile import write_mo
        locale_dir = Path(self.temp.name) / "locales"
        for locale in ("ru", "en"):
            target = locale_dir / locale / "LC_MESSAGES" / "bot.mo"
            target.parent.mkdir(parents=True)
            with (DEFAULT_LOCALES_DIR / locale / "LC_MESSAGES/bot.po").open("rb") as source:
                catalog = read_po(source)
            with target.open("wb") as output:
                write_mo(output, catalog)
        self.i18n = I18n(path=locale_dir, default_locale="ru", domain="bot")
        self.dev_patch = patch.object(IsDev, "developer_id", -1, create=True)
        self.admin_patch = patch.object(IsAdmin, "admins_ids", [])
        self.dev_patch.start()
        self.admin_patch.start()
        self.config = NS(bot=NS(DEV_ID=-1, ADMINS=[]),
                         shop=NS(CURRENCY="XTR", REFERRER_REWARD_ENABLED=True),
                         xui=NS(INBOUND_ID=6, SUBSCRIPTION_BASE_URL="https://subscription.example.test/custom/"))
        self.inbound = XUIInboundSummary.from_api(json.loads(
            (Path(__file__).parent / "fixtures/xui/v3_9_0/inbound_xhttp.json").read_text()))
        # Fixture ID is independent of production: make the explicit test ID 6.
        self.inbound = XUIInboundSummary.from_api({**self.inbound.raw, "id": 6})
        self.canonical = None
        self.pending = False
        self.panel_failure = False
        self.adapter = NS(add_client=AsyncMock(side_effect=self.add),
                          update_client=AsyncMock(side_effect=self.update),
                          get_client=AsyncMock(side_effect=self.get_client),
                          get_client_traffic=AsyncMock(side_effect=self.get_traffic),
                          get_inbound=AsyncMock(return_value=self.inbound))
        async with self.sessions() as session:
            self.server = Server(id=1, name="Offline", host="https://panel.example.test", max_clients=10, online=True, users=[])
            session.add(self.server)
            await session.commit()
        self.pool = ServerPoolService(self.config, self.sessions)
        self.pool._servers[1] = Connection(self.server, self.adapter)
        # Only external panel health I/O is replaced; pool selection/assignment are real.
        self.pool.sync_servers = AsyncMock()
        self.pool.validate_configured_inbound = AsyncMock(return_value=self.inbound)
        self.vpn = VPNService(self.config, self.sessions, self.pool)
        plan = Plan(devices=1, prices={"XTR": {30: 50}})
        self.services = NS(vpn=self.vpn, server_pool=self.pool,
            plan=NS(get_all_plans=lambda: [plan], get_plan=lambda n: plan if n == 1 else None,
                    get_durations=lambda: [30]),
            subscription=NS(is_trial_available=AsyncMock(return_value=False)),
            referral=NS(is_referred_trial_available=AsyncMock(return_value=False),
                        add_referrers_rewards_on_payment=AsyncMock()),
            notification=NotificationService(self.config, self.bot))
        self.gateway = TelegramStars(None, self.config, self.sessions, self.storage,
                                     self.bot, self.i18n, self.services)
        self.factory = NS(get_gateways=lambda: [self.gateway], get_gateway=self.get_gateway)
        self.dispatcher = Dispatcher(storage=self.storage)
        self.dispatcher.update.outer_middleware(DBSessionMiddleware(self.sessions))
        # Fresh routers retain actual registered filters, including admin authorization.
        for source in (main.router, subscription.router, payment.router, admin.router):
            router = Router()
            for observer_name in ("message", "callback_query", "pre_checkout_query"):
                for handler in getattr(source, observer_name).handlers:
                    getattr(router, observer_name).register(handler.callback,
                        *(f.callback for f in handler.filters))
            self.dispatcher.include_router(router)
        self.sequence = 0
        self.network_patch = patch("socket.socket.connect", side_effect=AssertionError("External network forbidden"))
        self.network_patch.start()

    async def asyncTearDown(self):
        self.network_patch.stop()
        self.dev_patch.stop()
        self.admin_patch.stop()
        await self.storage.close()
        await self.bot.session.close()
        await self.engine.dispose()
        self.temp.cleanup()

    def get_gateway(self, method):
        if method != Nav.PAY_TELEGRAM_STARS:
            raise ValueError("Unknown offline payment method")
        return self.gateway

    async def get_client(self, email):
        if self.canonical is None:
            raise XUINotFoundError("Client not found")
        return self.canonical

    async def get_traffic(self, email):
        return XUIClientTraffic.from_api(dict(id=999, email=email, up=10, down=20,
            total=0, expiryTime=self.canonical.expiry_time_ms, enable=True, inboundId=6))

    def persist(self, write):
        payload = write.client_payload()
        payload["uuid"] = payload.pop("id")
        payload["id"] = 81  # Numeric record ID deliberately differs from credential UUID.
        self.canonical = XUIClient.from_api({"client": payload, "inboundIds": list(write.inbound_ids)})
        return XUIWriteResult(success=True, node_pending=self.pending, reconciled=True, client=self.canonical)

    async def add(self, write):
        if self.panel_failure:
            raise RuntimeError("private-provider-payload")
        return self.persist(write)

    async def update(self, before, write):
        self.assertEqual(before.uuid, write.uuid)
        self.assertEqual(before.inbound_ids, write.inbound_ids)
        return self.persist(write)

    async def feed(self, event, **fields):
        self.sequence += 1
        payload = {"update_id": self.sequence, event: fields}
        with self.i18n.context(), self.i18n.use_locale("ru"):
            return await self.dispatcher.feed_update(self.bot, Update.model_validate(payload),
                config=self.config, services=self.services, gateway_factory=self.factory)

    @staticmethod
    def tg_user():
        return {"id": 700, "is_bot": False, "first_name": "Offline", "language_code": "ru"}

    async def message(self, **fields):
        return await self.feed("message", message_id=10, date=0,
            chat={"id": 700, "type": "private"}, from_user=self.tg_user(), **fields)

    async def callback(self, data, identity=None):
        return await self.feed("callback_query", id=identity or f"cb-{self.sequence}",
            from_user=self.tg_user(), chat_instance="offline",
            message={"message_id": 100, "date": 1, "chat": {"id": 700, "type": "private"}}, data=data)

    async def rows(self):
        async with self.sessions() as session:
            return list((await session.scalars(select(Transaction))).all())

    def last_keyboard(self):
        return next(m.reply_markup for m in reversed(self.sender.calls)
                    if m.__api_method__ in ("sendMessage", "editMessageText"))

    async def order(self):
        await self.message(text="/start", entities=[{"type": "bot_command", "offset": 0, "length": 6}])
        await self.callback(Nav.MAIN)
        await self.callback(self.last_keyboard().inline_keyboard[0][0].callback_data)
        await self.callback(self.last_keyboard().inline_keyboard[0][0].callback_data)
        await self.callback(self.last_keyboard().inline_keyboard[0][0].callback_data)
        self.pay_data = self.last_keyboard().inline_keyboard[0][0].callback_data
        await self.callback(self.pay_data, "payment-selection")
        rows = await self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].status, TransactionStatus.PENDING)
        self.assertEqual(rows[0].expected_amount, "50")
        self.assertEqual(rows[0].expected_currency, "XTR")
        self.assertTrue(any(b.url for row in self.last_keyboard().inline_keyboard for b in row))
        return rows[0]

    async def paid(self, order, **overrides):
        details = dict(currency="XTR", total_amount=50, invoice_payload=order.payment_id,
                       telegram_payment_charge_id="offline-charge-" + order.payment_id, provider_payment_charge_id="")
        details.update(overrides)
        await self.message(successful_payment=details)

    async def test_start_tariff_order_checkout_success_and_duplicate(self):
        row = await self.order()
        await self.feed("pre_checkout_query", id="checkout", from_user=self.tg_user(),
                        currency="XTR", total_amount=50, invoice_payload=row.payment_id)
        self.assertTrue(self.sender.calls[-1].ok)
        await self.paid(row)
        await self.paid(row)
        self.assertEqual((await self.rows())[0].status, TransactionStatus.COMPLETED)
        self.adapter.add_client.assert_awaited_once()
        self.services.referral.add_referrers_rewards_on_payment.assert_awaited_once()
        self.assertTrue(any(m.__api_method__ == "sendMessage" and "https://subscription.example.test/custom/" in m.text
                            for m in self.sender.calls))

    async def test_pending_and_ambiguous_are_review_without_success_or_duplicate(self):
        for pending in (True, None):
            with self.subTest(node_pending=pending):
                # Independent orders, identical user; fake panel reset only.
                self.pending = pending
                data = SubscriptionData(state=Nav.PAY_TELEGRAM_STARS, user_id=700, devices=1, duration=30, price=50)
                await self.message(text="/start", entities=[{"type": "bot_command", "offset": 0, "length": 6}])
                with self.i18n.context():
                    await self.gateway.create_payment(data)
                row = (await self.rows())[-1]
                before = len(self.sender.calls)
                await self.paid(row)
                await self.paid(row)
                self.assertEqual((await self.rows())[-1].status, TransactionStatus.REVIEW_REQUIRED)
                self.assertEqual(len(self.sender.calls), before + 1)
                text = self.sender.calls[-1].text
                self.assertIn("ручную проверку", text)
                self.assertIn("Повторно оплачивать не нужно", text)
                self.assertNotIn("nodePending", text)
                self.assertNotIn("REVIEW_REQUIRED", text)
        self.assertEqual(self.adapter.add_client.await_count, 2)
        self.services.referral.add_referrers_rewards_on_payment.assert_not_awaited()

    async def test_provisioning_failure_keeps_review_and_no_false_success(self):
        row = await self.order()
        self.panel_failure = True
        before = len(self.sender.calls)
        await self.paid(row)
        await self.paid(row)
        self.assertEqual((await self.rows())[0].status, TransactionStatus.REVIEW_REQUIRED)
        self.assertEqual(len(self.sender.calls), before + 1)
        self.assertIn("ручную проверку", self.sender.calls[-1].text)
        self.adapter.add_client.assert_awaited_once()
        self.services.referral.add_referrers_rewards_on_payment.assert_not_awaited()

    async def test_review_notification_works_outside_telegram_i18n_middleware(self):
        row = await self.order()
        self.pending = None
        before = len(self.sender.calls)
        await self.gateway.handle_payment_succeeded(row.payment_id)
        self.assertEqual((await self.rows())[0].status, TransactionStatus.REVIEW_REQUIRED)
        self.assertEqual(len(self.sender.calls), before + 1)
        self.assertIn("ручную проверку", self.sender.calls[-1].text)

    async def test_review_notification_unknown_language_uses_readable_english(self):
        row = await self.order()
        async with self.sessions() as session:
            await User.update(session, 700, language_code="unknown")
        self.pending = True
        await self.gateway.handle_payment_succeeded(row.payment_id)
        text = self.sender.calls[-1].text
        self.assertIn("Payment received", text)
        self.assertIn("do not pay again", text)
        self.assertNotIn("review_required", text)

    async def test_renewal_updates_preserving_identity_and_memberships(self):
        row = await self.order()
        await self.paid(row)
        old = self.canonical
        await self.callback(Nav.MAIN)
        await self.callback(self.last_keyboard().inline_keyboard[0][0].callback_data)
        await self.callback(self.last_keyboard().inline_keyboard[0][0].callback_data)
        await self.callback(self.last_keyboard().inline_keyboard[0][0].callback_data)
        renewal = (await self.rows())[-1]
        await self.paid(renewal, telegram_payment_charge_id="offline-renewal-charge")
        self.adapter.add_client.assert_awaited_once()
        self.adapter.update_client.assert_awaited_once()
        self.assertEqual(self.canonical.uuid, old.uuid)
        self.assertEqual(self.canonical.sub_id, old.sub_id)
        self.assertEqual(self.canonical.inbound_ids, old.inbound_ids)
        self.assertEqual(self.canonical.expiry_time_ms, old.expiry_time_ms + 30 * 86400000)
        self.assertEqual((await self.rows())[-1].status, TransactionStatus.COMPLETED)

    async def test_concurrent_success_deliveries_claim_once(self):
        row = await self.order()
        await asyncio.gather(self.paid(row), self.paid(row))
        self.assertEqual((await self.rows())[0].status, TransactionStatus.COMPLETED)
        self.adapter.add_client.assert_awaited_once()
        self.services.referral.add_referrers_rewards_on_payment.assert_awaited_once()

    async def test_wrong_amount_and_currency_do_not_claim(self):
        row = await self.order()
        await self.paid(row, total_amount=51)
        await self.paid(row, currency="USD")
        self.assertEqual((await self.rows())[0].status, TransactionStatus.PENDING)
        self.adapter.add_client.assert_not_awaited()

    async def test_native_webhook_secret_gate_before_dispatch(self):
        from aiogram.webhook.aiohttp_server import SimpleRequestHandler
        secret = "synthetic-webhook-secret"
        dispatcher = NS(feed_webhook_update=AsyncMock(return_value=None))
        handler = SimpleRequestHandler(dispatcher=dispatcher, bot=self.bot,
                                       secret_token=secret, handle_in_background=False)
        for headers in ({}, {"X-Telegram-Bot-Api-Secret-Token": "wrong"}):
            request = NS(headers=headers, json=AsyncMock(return_value={"update_id": 1}))
            self.assertEqual((await handler.handle(request)).status, 401)
            request.json.assert_not_awaited()
        dispatcher.feed_webhook_update.assert_not_awaited()
        request = NS(headers={"X-Telegram-Bot-Api-Secret-Token": secret},
                     json=AsyncMock(return_value={"update_id": 1}))
        self.assertEqual((await handler.handle(request)).status, 200)
        dispatcher.feed_webhook_update.assert_awaited_once()
        self.assertEqual(self.sender.calls, [])

    async def test_user_messages_do_not_expose_internal_errors(self):
        row = await self.order()
        self.panel_failure = True
        await self.paid(row)
        texts = "\n".join(str(getattr(m, "text", "")) for m in self.sender.calls
                          if getattr(m, "chat_id", None) == 700)
        for forbidden in ("Traceback", "private-provider-payload", self.bot.token,
                          "PROCESSING", "REVIEW_REQUIRED", row.payment_id):
            self.assertNotIn(forbidden, texts)

    async def test_invalid_order_user_admin_and_unexpected_update_do_not_mutate(self):
        await self.message(text="/start", entities=[{"type": "bot_command", "offset": 0, "length": 6}])
        data = SubscriptionData(state=Nav.PAY_TELEGRAM_STARS, user_id=701, devices=1, duration=30)
        await self.callback(data.pack())
        await self.callback("admin_tools")
        await self.callback("subscription:malformed")
        await self.message(text="unrecognized text")
        await self.paid(NS(payment_id="unknown-order"))
        self.assertEqual(await self.rows(), [])
        self.adapter.add_client.assert_not_awaited()
        self.adapter.update_client.assert_not_awaited()
        self.assertFalse(any("admin_tools" in str(m) for m in self.sender.calls))

    async def test_duplicate_payment_selection_reuses_one_order_and_payment_action(self):
        await self.order()
        await self.callback(self.pay_data, "payment-selection")
        self.assertEqual(len(await self.rows()), 1)
        self.assertEqual(sum(m.__api_method__ == "createInvoiceLink" for m in self.sender.calls), 1)
        self.assertEqual(self.last_keyboard().inline_keyboard[0][0].url,
                         "https://invoice.example.test/local-order")
        self.adapter.add_client.assert_not_awaited()

    async def test_concurrent_payment_selection_calls_invoice_once(self):
        # Generate a real checkout button, then redeliver it concurrently.
        await self.order()
        first = (await self.rows())[0]
        # Explicit buy action starts a new flow; navigating back must not.
        await self.callback(SubscriptionData(state=Nav.PROCESS, user_id=700).pack())
        await self.callback(self.last_keyboard().inline_keyboard[0][0].callback_data)
        await self.callback(self.last_keyboard().inline_keyboard[0][0].callback_data)
        checkout = self.last_keyboard().inline_keyboard[0][0].callback_data
        self.assertLessEqual(len(checkout.encode()), 64)
        await asyncio.gather(self.callback(checkout, "same-payment-id"),
                             self.callback(checkout, "same-payment-id"))
        rows = await self.rows()
        self.assertEqual(len(rows), 2)  # One old flow and exactly one new flow.
        self.assertNotEqual(rows[0].purchase_flow_id, rows[1].purchase_flow_id)
        self.assertEqual(sum(m.__api_method__ == "createInvoiceLink" for m in self.sender.calls), 2)
        self.assertEqual(rows[0].payment_id, first.payment_id)

    async def test_back_to_payment_method_preserves_checkout_identity(self):
        row = await self.order()
        back = self.last_keyboard().inline_keyboard[1][0].callback_data
        await self.callback(back)
        checkout = self.last_keyboard().inline_keyboard[0][0].callback_data
        self.assertEqual(CheckoutData.unpack(checkout).flow_id, row.purchase_flow_id)
        await self.callback(checkout)
        self.assertEqual(len(await self.rows()), 1)
        self.assertEqual(sum(m.__api_method__ == "createInvoiceLink" for m in self.sender.calls), 1)

    async def test_duplicate_reuses_db_action_after_fsm_loss(self):
        await self.order()
        await self.storage.close()
        self.storage.storage.clear()
        await self.callback(self.pay_data, "replayed-after-restart")
        self.assertEqual(len(await self.rows()), 1)
        self.assertEqual(sum(m.__api_method__ == "createInvoiceLink" for m in self.sender.calls), 1)
        self.assertEqual(self.last_keyboard().inline_keyboard[0][0].url,
                         "https://invoice.example.test/local-order")

    async def test_completed_processing_review_do_not_recreate_invoice(self):
        row = await self.order()
        for status in (TransactionStatus.PROCESSING, TransactionStatus.REVIEW_REQUIRED,
                       TransactionStatus.COMPLETED):
            async with self.sessions() as session:
                await session.execute(Transaction.__table__.update().where(
                    Transaction.payment_id == row.payment_id).values(status=status))
                await session.commit()
            await self.callback(self.pay_data)
        self.assertEqual(len(await self.rows()), 1)
        self.assertEqual(sum(m.__api_method__ == "createInvoiceLink" for m in self.sender.calls), 1)
        self.adapter.add_client.assert_not_awaited()

    async def test_all_providers_reserve_before_invoice_and_reuse_across_instances(self):
        """Actual provider request builders; all transports locally patched."""
        from app.bot.payment_gateways import Cryptomus, Heleket, Yookassa, Yoomoney
        from app.bot.payment_gateways import cryptomus, heleket, yookassa, yoomoney
        await self.message(text="/start", entities=[{"type": "bot_command", "offset": 0, "length": 6}])
        self.config.bot.DOMAIN = "https://shop.example.test"
        self.config.shop.EMAIL = "offline@example.test"
        self.config.yoomoney = NS(WALLET_ID="synthetic-wallet")
        self.config.cryptomus = NS(API_KEY="synthetic-key", MERCHANT_ID="synthetic-merchant")
        self.config.heleket = NS(API_KEY="synthetic-key", MERCHANT_ID="synthetic-merchant")

        class LocalResponse:
            status = 200
            def __init__(self, payload):
                self.payload = payload
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            async def json(self):
                return {"result": {"url": "https://invoice.example.test/local",
                    "order_id": self.payload["order_id"], "uuid": str(uuid.uuid4())}}

        class LocalHTTP:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            def post(self, url, *, json, headers):
                return LocalResponse(json)

        for cls in (TelegramStars, Yookassa, Yoomoney, Cryptomus, Heleket):
            with self.subTest(provider=cls.provider):
                gateway = object.__new__(cls)
                gateway.__dict__.update(self.gateway.__dict__)
                gateway.bot = NS(get_me=AsyncMock(return_value=NS(username="offline_bot")),
                                 create_invoice_link=self.bot.create_invoice_link)
                data = SubscriptionData(state=gateway.callback, user_id=700, devices=1,
                                        duration=30, price=50)
                flow_id = uuid.uuid4().hex
                original = gateway._create_invoice

                async def check_reservation(data, reserved):
                    async with self.sessions() as session:
                        persisted = await Transaction.get_by_purchase_flow(session, flow_id)
                    self.assertEqual(persisted.status, TransactionStatus.REVIEW_REQUIRED)
                    self.assertIsNone(persisted.payment_url)
                    self.assertEqual(persisted.purchase_flow_id, flow_id)
                    await asyncio.sleep(0)  # Allow another engine/connection to race.
                    return await original(data, reserved)

                gateway._create_invoice = AsyncMock(side_effect=check_reservation)
                remote_id = str(uuid.uuid4())
                with self.i18n.context(), patch.object(cryptomus.aiohttp, "ClientSession", LocalHTTP), \
                        patch.object(heleket.aiohttp, "ClientSession", LocalHTTP), \
                        patch.object(yoomoney.requests, "post", return_value=NS(url="https://invoice.example.test/local")), \
                        patch.object(yookassa.Payment, "create", return_value=NS(
                            id=remote_id, confirmation={"confirmation_url": "https://invoice.example.test/local"})):
                    results = await asyncio.gather(gateway.create_payment(data, flow_id),
                                                   gateway.create_payment(data, flow_id), return_exceptions=True)
                    action = next(r for r in results if isinstance(r, str))
                    self.assertTrue(all(isinstance(r, (str, CheckoutUnavailable)) for r in results))
                    gateway._create_invoice.assert_awaited_once()
                    # Fresh gateway AND engine: no process-local lock/FSM state.
                    second_engine = create_async_engine(self.engine.url)
                    try:
                        restarted = object.__new__(cls)
                        restarted.__dict__.update(gateway.__dict__)
                        restarted.session = async_sessionmaker(second_engine, expire_on_commit=False)
                        restarted._create_invoice = AsyncMock(side_effect=AssertionError("Invoice replay forbidden"))
                        self.assertEqual(await restarted.create_payment(data, flow_id), action)
                        restarted._create_invoice.assert_not_awaited()
                    finally:
                        await second_engine.dispose()
                matches = [r for r in await self.rows() if r.purchase_flow_id == flow_id]
                self.assertEqual(len(matches), 1)
                self.assertEqual(matches[0].status, TransactionStatus.PENDING)
                self.assertEqual(matches[0].payment_url, action)
                if cls is Yookassa:
                    self.assertEqual(matches[0].payment_id, remote_id)

    async def test_creation_failure_and_crash_leave_nonpayable_reservation_no_retry(self):
        await self.message(text="/start", entities=[{"type": "bot_command", "offset": 0, "length": 6}])
        for error in (TimeoutError("synthetic-provider-secret"), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):
                flow_id = uuid.uuid4().hex
                data = SubscriptionData(state=Nav.PAY_TELEGRAM_STARS, user_id=700,
                                        devices=1, duration=30, price=50)
                with self.i18n.context(), patch.object(self.gateway, "_create_invoice", AsyncMock(side_effect=error)) as invoice:
                    expected = asyncio.CancelledError if isinstance(error, asyncio.CancelledError) else CheckoutUnavailable
                    with self.assertRaises(expected):
                        await self.gateway.create_payment(data, flow_id)
                    with self.assertRaises(CheckoutUnavailable):
                        await self.gateway.create_payment(data, flow_id)
                    invoice.assert_awaited_once()
                row = next(r for r in await self.rows() if r.purchase_flow_id == flow_id)
                self.assertEqual(row.status, TransactionStatus.REVIEW_REQUIRED)
                self.assertIsNone(row.payment_url)
                with self.assertRaises(InvalidPayment):
                    await self.gateway.validate_checkout(row.payment_id, 700, 50, "XTR")

    async def test_legacy_and_stale_checkout_buttons_never_create(self):
        await self.message(text="/start", entities=[{"type": "bot_command", "offset": 0, "length": 6}])
        await self.callback(SubscriptionData(state=Nav.PAY_TELEGRAM_STARS,
            user_id=700, devices=1, duration=30).pack())
        await self.callback(CheckoutData(flow_id=uuid.uuid4().hex, provider="stars").pack())
        self.assertEqual(await self.rows(), [])
        self.assertEqual(sum(m.__api_method__ == "createInvoiceLink" for m in self.sender.calls), 0)

    async def test_error_handler_only_reports_sanitized_metadata(self):
        from aiogram.types import ErrorEvent
        from app.bot.routers.misc import error_handler
        update = Update.model_validate({"update_id": 99, "message": {
            "message_id": 1, "date": 1, "chat": {"id": 700, "type": "private"},
            "from": self.tg_user(), "successful_payment": {
                "currency": "XTR", "total_amount": 50, "invoice_payload": "synthetic-private-order",
                "telegram_payment_charge_id": "synthetic-private-charge", "provider_payment_charge_id": ""}}})
        notification = NS(notify_developer=AsyncMock())
        with patch.object(error_handler.logger, "error") as logged:
            await error_handler.errors_handler(ErrorEvent(update=update, exception=RuntimeError("synthetic-private-charge")),
                                               self.config, NS(notification=notification))
        diagnostics = str(logged.call_args) + str(notification.notify_developer.call_args_list)
        for secret in ("synthetic-private-charge", "synthetic-private-order", "successful_payment",
                       "invoice_payload", "telegram_payment_charge_id"):
            self.assertNotIn(secret, diagnostics)
        for field in ("update_id=99", "type=message", "user_id=700", "chat_id=700", "exception_type=RuntimeError"):
            self.assertIn(field, diagnostics)
        notification.notify_developer.assert_awaited_once()
        self.assertEqual(set(notification.notify_developer.call_args.kwargs), {"text"})

    async def test_error_diagnostics_redact_message_callback_checkout_and_chained_exception(self):
        import traceback
        from aiogram.types import ErrorEvent
        from app.bot.routers.misc import error_handler
        private = "synthetic-sensitive-value"
        try:
            try:
                raise ValueError(private)
            except ValueError as cause:
                raise RuntimeError(private) from cause
        except RuntimeError as exception:
            failure = exception
        variants = (
            {"message": {"message_id": 1, "date": 1, "chat": {"id": 700, "type": "private"},
                         "from": self.tg_user(), "text": private}},
            {"pre_checkout_query": {"id": "checkout-private", "from": self.tg_user(),
                                    "currency": "XTR", "total_amount": 50, "invoice_payload": private}},
            {"callback_query": {"id": "callback-private", "from": self.tg_user(),
                                "chat_instance": "offline", "data": private,
                                "message": {"message_id": 1, "date": 1,
                                            "chat": {"id": 700, "type": "private"}}}},
        )
        notification = NS(notify_developer=AsyncMock())
        for variant in variants:
            with self.subTest(update_type=next(iter(variant))):
                update = Update.model_validate({"update_id": 999, **variant})
                with patch.object(error_handler.logger, "error") as logged:
                    await error_handler.errors_handler(ErrorEvent(update=update, exception=failure),
                                                       self.config, NS(notification=notification))
                self.assertNotIn(private, repr(logged.call_args))
                rendered = "".join(traceback.format_exception(*logged.call_args.kwargs["exc_info"]))
                self.assertIn("test_error_diagnostics", rendered)  # Real stack survives.
                self.assertNotIn(private, rendered)
                self.assertNotIn(private, repr(notification.notify_developer.call_args))
                self.assertIn("user_id=700", notification.notify_developer.call_args.kwargs["text"])

    async def test_finalize_failure_never_retries_provider_invoice(self):
        await self.message(text="/start", entities=[{"type": "bot_command", "offset": 0, "length": 6}])
        data = SubscriptionData(state=Nav.PAY_TELEGRAM_STARS, user_id=700,
                                devices=1, duration=30, price=50)
        flow_id = uuid.uuid4().hex
        with self.i18n.context(), patch.object(Transaction, "finish_invoice", AsyncMock(side_effect=RuntimeError("DB offline"))):
            with self.assertRaises(CheckoutUnavailable):
                await self.gateway.create_payment(data, flow_id)
        with self.i18n.context(), self.assertRaises(CheckoutUnavailable):
            await self.gateway.create_payment(data, flow_id)
        self.assertEqual(sum(m.__api_method__ == "createInvoiceLink" for m in self.sender.calls), 1)
        row = (await self.rows())[0]
        self.assertEqual(row.status, TransactionStatus.REVIEW_REQUIRED)
        self.assertIsNone(row.payment_url)

    async def test_foreign_user_provider_or_cart_cannot_reuse_flow(self):
        row = await self.order()
        data = SubscriptionData.unpack(row.subscription)
        for changes in ({"user_id": 701}, {"devices": 2}, {"duration": 60}):
            with self.subTest(changes=changes), self.i18n.context(), self.assertRaises(InvalidPayment):
                await self.gateway.create_payment(data.model_copy(update=changes), row.purchase_flow_id)
        from app.bot.payment_gateways import Yoomoney
        gateway = object.__new__(Yoomoney)
        gateway.__dict__.update(self.gateway.__dict__)
        other = data.model_copy(update={"state": gateway.callback})
        with self.i18n.context(), self.assertRaises(InvalidPayment):
            await gateway.create_payment(other, row.purchase_flow_id)
        self.assertEqual(len(await self.rows()), 1)
        self.assertEqual(sum(m.__api_method__ == "createInvoiceLink" for m in self.sender.calls), 1)
