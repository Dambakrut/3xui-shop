"""Payment callback tests with local fakes; no provider, Telegram, or 3x-ui calls."""

import asyncio
import ast
import logging
import unittest
from abc import ABC, abstractmethod
from contextlib import nullcontext
from enum import Enum
from types import SimpleNamespace
from unittest.mock import AsyncMock
from weakref import WeakValueDictionary

from test_safe_inbound import ROOT, SessionContext, load_definitions


class Status(Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    REVIEW_REQUIRED = "review_required"
    CANCELED = "canceled"
    REFUNDED = "refunded"


class PaymentIdempotencyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.data = SimpleNamespace(
            user_id=123, price=100, devices=2, duration=30,
            is_extend=True, is_change=False,
        )
        self.user = SimpleNamespace(tg_id=123, language_code="en")
        self.transaction = SimpleNamespace(
            payment_id="payment-1", subscription="order-1", tg_id=123,
            status=Status.PENDING,
        )
        self.records = {"payment-1": self.transaction}
        records = self.records
        parent = self
        self.completion_error = False
        self.review_error = False

        class FakeTransaction:
            @classmethod
            async def get_by_id(cls, session, payment_id):
                return records.get(payment_id)

            @classmethod
            async def set_status_if_pending(cls, session, payment_id, status):
                transaction = records.get(payment_id)
                if not transaction or transaction.status != Status.PENDING:
                    return False
                transaction.status = status
                return True

            @classmethod
            async def set_status_if_processing(cls, session, payment_id, status, updated_before=None):
                transaction = records.get(payment_id)
                if status == Status.COMPLETED and parent.completion_error:
                    raise RuntimeError("DB completion unavailable")
                if status == Status.REVIEW_REQUIRED and parent.review_error:
                    raise RuntimeError("DB review unavailable")
                if not transaction or transaction.status != Status.PROCESSING:
                    return False
                transaction.status = status
                return True

        self.vpn = SimpleNamespace(
            create_subscription=AsyncMock(return_value=True),
            extend_subscription=AsyncMock(return_value=True),
            change_subscription=AsyncMock(return_value=True),
            get_key=AsyncMock(return_value="subscription-url"),
        )
        self.referral = SimpleNamespace(add_referrers_rewards_on_payment=AsyncMock())
        self.notification = SimpleNamespace(
            notify_developer=AsyncMock(), notify_extend_success=AsyncMock(),
            notify_change_success=AsyncMock(), notify_purchase_success=AsyncMock(),
        )
        self.redirect = AsyncMock()
        namespace = dict(
            asyncio=asyncio, ABC=ABC, abstractmethod=abstractmethod,
            WeakValueDictionary=WeakValueDictionary,
            _payment_locks=WeakValueDictionary(),
            logger=logging.getLogger("payment-idempotency-test"),
            Transaction=FakeTransaction,
            TransactionStatus=Status,
            SubscriptionData=SimpleNamespace(unpack=lambda raw: self.data),
            User=SimpleNamespace(get=AsyncMock(return_value=self.user)),
            redirect_to_main_menu=self.redirect,
            DEFAULT_LANGUAGE="en", EVENT_PAYMENT_SUCCEEDED_TAG="success",
            EVENT_PAYMENT_CANCELED_TAG="canceled",
            _=lambda message: message,
            format_device_count=str, format_subscription_period=str,
        )
        load_definitions(
            "app/bot/payment_gateways/_gateway.py",
            {"_payment_lock", "PaymentGateway"}, namespace,
        )

        class Gateway(namespace["PaymentGateway"]):
            async def create_payment(self, data):
                return ""

            async def handle_payment_succeeded(self, payment_id):
                await self._on_payment_succeeded(payment_id)

            async def handle_payment_canceled(self, payment_id):
                await self._on_payment_canceled(payment_id)

        self.gateway = Gateway(
            app=None,
            config=SimpleNamespace(shop=SimpleNamespace(REFERRER_REWARD_ENABLED=True)),
            session=SessionContext,
            storage=None, bot=None,
            i18n=SimpleNamespace(use_locale=lambda locale: nullcontext()),
            services=SimpleNamespace(
                vpn=self.vpn, referral=self.referral, notification=self.notification,
            ),
        )

    async def test_first_pending_payment_provisions_then_completes(self):
        async def provision(**kwargs):
            self.assertEqual(self.transaction.status, Status.PROCESSING)
            return True

        self.vpn.extend_subscription.side_effect = provision
        await self.gateway.handle_payment_succeeded("payment-1")
        self.vpn.extend_subscription.assert_awaited_once()
        self.assertEqual(self.transaction.status, Status.COMPLETED)

    async def test_completed_duplicate_has_no_side_effects(self):
        self.transaction.status = Status.COMPLETED
        await self.gateway.handle_payment_succeeded("payment-1")
        self.vpn.extend_subscription.assert_not_awaited()
        self.referral.add_referrers_rewards_on_payment.assert_not_awaited()
        self.notification.notify_developer.assert_not_awaited()

    async def test_two_sequential_callbacks_provision_and_reward_once(self):
        await self.gateway.handle_payment_succeeded("payment-1")
        await self.gateway.handle_payment_succeeded("payment-1")
        self.vpn.extend_subscription.assert_awaited_once()
        self.referral.add_referrers_rewards_on_payment.assert_awaited_once()
        self.notification.notify_extend_success.assert_awaited_once()

    async def test_provisioning_failure_requires_review(self):
        self.vpn.extend_subscription.return_value = False
        await self.gateway.handle_payment_succeeded("payment-1")
        self.assertEqual(self.transaction.status, Status.REVIEW_REQUIRED)
        self.referral.add_referrers_rewards_on_payment.assert_not_awaited()

    async def test_duplicate_after_provisioning_failure_does_not_retry(self):
        self.vpn.extend_subscription.side_effect = [False, True]
        await self.gateway.handle_payment_succeeded("payment-1")
        self.assertEqual(self.transaction.status, Status.REVIEW_REQUIRED)
        await self.gateway.handle_payment_succeeded("payment-1")
        self.assertEqual(self.transaction.status, Status.REVIEW_REQUIRED)
        self.vpn.extend_subscription.assert_awaited_once()
        self.referral.add_referrers_rewards_on_payment.assert_not_awaited()

    async def test_provisioning_exception_requires_review(self):
        self.vpn.extend_subscription.side_effect = TimeoutError("unknown outcome")
        await self.gateway.handle_payment_succeeded("payment-1")
        self.assertEqual(self.transaction.status, Status.REVIEW_REQUIRED)
        self.referral.add_referrers_rewards_on_payment.assert_not_awaited()

    async def test_completion_db_failure_does_not_return_to_pending(self):
        self.completion_error = True
        await self.gateway.handle_payment_succeeded("payment-1")
        self.assertEqual(self.transaction.status, Status.REVIEW_REQUIRED)
        await self.gateway.handle_payment_succeeded("payment-1")
        self.vpn.extend_subscription.assert_awaited_once()
        self.referral.add_referrers_rewards_on_payment.assert_not_awaited()

    async def test_persistent_db_failure_leaves_processing_until_recovery(self):
        self.completion_error = True
        self.review_error = True
        await self.gateway.handle_payment_succeeded("payment-1")
        self.assertEqual(self.transaction.status, Status.PROCESSING)
        await self.gateway.handle_payment_succeeded("payment-1")
        self.vpn.extend_subscription.assert_awaited_once()

    async def test_processing_duplicate_does_not_provision(self):
        self.transaction.status = Status.PROCESSING
        await self.gateway.handle_payment_succeeded("payment-1")
        self.vpn.extend_subscription.assert_not_awaited()

    async def test_review_duplicate_does_not_provision(self):
        self.transaction.status = Status.REVIEW_REQUIRED
        await self.gateway.handle_payment_succeeded("payment-1")
        self.vpn.extend_subscription.assert_not_awaited()

    async def test_missing_or_invalid_transaction_does_not_provision(self):
        await self.gateway.handle_payment_succeeded("missing")
        await self.gateway.handle_payment_succeeded("")
        self.transaction.tg_id = 999
        await self.gateway.handle_payment_succeeded("payment-1")
        self.vpn.extend_subscription.assert_not_awaited()

    async def test_canceled_and_refunded_are_not_provisioned(self):
        for status in (Status.CANCELED, Status.REFUNDED):
            self.transaction.status = status
            await self.gateway.handle_payment_succeeded("payment-1")
            self.assertEqual(self.transaction.status, status)
        self.vpn.extend_subscription.assert_not_awaited()

    async def test_cancel_callback_cannot_overwrite_completed(self):
        self.transaction.status = Status.COMPLETED
        await self.gateway.handle_payment_canceled("payment-1")
        self.assertEqual(self.transaction.status, Status.COMPLETED)
        self.notification.notify_developer.assert_not_awaited()

    async def test_concurrent_callbacks_in_one_process_provision_once(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def provision(**kwargs):
            started.set()
            await release.wait()
            return True

        self.vpn.extend_subscription.side_effect = provision
        first = asyncio.create_task(self.gateway.handle_payment_succeeded("payment-1"))
        await started.wait()
        second = asyncio.create_task(self.gateway.handle_payment_succeeded("payment-1"))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first, second)
        self.vpn.extend_subscription.assert_awaited_once()
        self.assertEqual(self.transaction.status, Status.COMPLETED)


class StarsDuplicateTests(unittest.IsolatedAsyncioTestCase):
    async def test_stars_duplicate_routes_same_saved_order_without_creating_transaction(self):
        source = ROOT / "app/bot/routers/subscription/payment_handler.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        handler = next(
            node for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "successful_payment"
        )
        handler.decorator_list = []
        module = ast.Module(
            body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), handler],
            type_ignores=[],
        )
        data = SimpleNamespace(user_id=123, pack=lambda: "order-1")
        transaction = SimpleNamespace(
            payment_id="charge-1", tg_id=123, subscription="order-1", status=Status.PENDING
        )
        records = {}

        class FakeTransaction:
            @classmethod
            async def create(cls, session, **kwargs):
                if kwargs["payment_id"] in records:
                    return None
                self.assertEqual(kwargs["status"], Status.PENDING)
                records[kwargs["payment_id"]] = transaction
                return transaction

            @classmethod
            async def get_by_id(cls, session, payment_id):
                return records.get(payment_id)

        class IsDev:
            async def __call__(self, user_id):
                return False

        gateway = SimpleNamespace(process_successful_payment=AsyncMock())
        namespace = dict(
            Transaction=FakeTransaction, TransactionStatus=Status,
            SubscriptionData=SimpleNamespace(unpack=lambda payload: data),
            IsDev=IsDev, NavSubscription=SimpleNamespace(PAY_TELEGRAM_STARS="stars"),
            logger=logging.getLogger("stars-duplicate-test"),
            InvalidPayment=ValueError,
        )
        exec(compile(ast.fix_missing_locations(module), str(source), "exec"), namespace)
        payment = SimpleNamespace(invoice_payload="order-1", telegram_payment_charge_id="charge-1", total_amount=100, currency="XTR")
        message = SimpleNamespace(successful_payment=payment, from_user=SimpleNamespace(id=123))
        factory = SimpleNamespace(get_gateway=lambda name: gateway)
        user = SimpleNamespace(tg_id=123)
        bot = SimpleNamespace(refund_star_payment=AsyncMock())
        for _ in range(2):
            await namespace["successful_payment"](
                message=message, user=user, session=object(), bot=bot, gateway_factory=factory
            )
        self.assertEqual(transaction.status, Status.PENDING)
        self.assertEqual(records, {})  # The router cannot create a new transaction from untrusted payload.
        self.assertEqual(gateway.process_successful_payment.await_count, 2)
        for call in gateway.process_successful_payment.await_args_list:
            self.assertEqual(call.args, ("order-1", 123, 100, "XTR", "charge-1"))


if __name__ == "__main__":
    unittest.main()
