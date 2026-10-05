"""Actual gateway methods with local DB/provider/Telegram fakes; no API calls."""
import asyncio
import ast
import base64
import hashlib
import hmac
import io
import json
import logging
import re
import unittest
import uuid
from abc import ABC, abstractmethod
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import quote
from urllib.parse import urljoin
from weakref import WeakValueDictionary

from app.bot.utils.constants import Currency, TransactionStatus
from app.bot.utils.navigation import NavSubscription
from app.bot.utils.payment_security import (
    CheckoutUnavailable, InvalidPayment, money, payment_snapshot, safe_client_ip, validate_order,
)
from test_safe_inbound import SessionContext, load_definitions, ROOT


class GatewaySecurityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.records = {}
        self.data = NS(user_id=123, state=None, devices=2, duration=30, price=100,
                       is_extend=True, is_change=False, pack=lambda: "saved-subscription")
        parent = self

        class FakeTransaction:
            @classmethod
            async def reserve_purchase_flow(cls, session, flow_id, **values):
                existing = next((r for r in parent.records.values()
                                 if getattr(r, "purchase_flow_id", None) == flow_id), None)
                if existing:
                    return existing, False
                row = NS(**values, purchase_flow_id=flow_id, payment_url=None,
                         status=TransactionStatus.REVIEW_REQUIRED)
                parent.records[row.payment_id] = row
                return row, True

            @classmethod
            async def finish_invoice(cls, session, flow_id, payment_id, payment_url, provider_payment_id):
                row = next(r for r in parent.records.values() if getattr(r, "purchase_flow_id", None) == flow_id)
                del parent.records[row.payment_id]
                row.payment_id, row.payment_url = payment_id, payment_url
                row.provider_payment_id = provider_payment_id
                row.status = TransactionStatus.PENDING
                parent.records[payment_id] = row
                return True

            @classmethod
            async def get_by_id(cls, session, payment_id):
                return parent.records.get(payment_id)

            @classmethod
            async def create(cls, session, **values):
                if values["payment_id"] in parent.records:
                    return None
                row = NS(**values)
                parent.records[values["payment_id"]] = row
                return row

            @classmethod
            async def bind_provider_payment_id(cls, session, payment_id, provider_id, only_if_unbound=False):
                row = parent.records[payment_id]
                if not isinstance(provider_id, str) or not provider_id or len(provider_id) > 128:
                    return False
                if row.provider_payment_id is not None:
                    return not only_if_unbound and row.provider_payment_id == provider_id
                if any(r.payment_provider == row.payment_provider and r.provider_payment_id == provider_id
                       for r in parent.records.values()):
                    return False
                row.provider_payment_id = provider_id
                return True

        self.model = FakeTransaction
        self.dev = False

        class IsDev:
            async def __call__(self, user_id):
                return parent.dev

        self.namespace = dict(
            asyncio=asyncio, logging=logging, re=re, logger=logging.getLogger("payment-security-test"),
            ABC=ABC, abstractmethod=abstractmethod, WeakValueDictionary=WeakValueDictionary,
            _payment_locks=WeakValueDictionary(),
            Transaction=FakeTransaction, TransactionStatus=TransactionStatus,
            SubscriptionData=NS(unpack=lambda raw: parent.data),
            CheckoutUnavailable=CheckoutUnavailable, InvalidPayment=InvalidPayment,
            validate_order=validate_order, money=money,
            payment_snapshot=payment_snapshot, safe_client_ip=safe_client_ip,
            Currency=Currency, NavSubscription=NavSubscription,
            base64=base64, hashlib=hashlib, hmac=hmac, json=json, uuid=uuid,
            compare_digest=hmac.compare_digest, quote=quote,
            Response=lambda status: NS(status=status), IsDev=IsDev,
            LabeledPrice=lambda **kwargs: NS(**kwargs),
            _=lambda message: "{devices} {duration}", __=lambda message: message,
            format_device_count=str, format_subscription_period=str,
            Payment=NS(find_one=Mock()),
        )
        load_definitions("app/bot/payment_gateways/_gateway.py", {"PaymentGateway", "_payment_lock"}, self.namespace)

    def gateway(self, provider):
        classes = {"stars": ("telegram_stars", "TelegramStars"), "yoomoney": ("yoomoney", "Yoomoney"),
                   "yookassa": ("yookassa", "Yookassa"), "cryptomus": ("cryptomus", "Cryptomus"),
                   "heleket": ("heleket", "Heleket")}
        module, name = classes[provider]
        load_definitions(f"app/bot/payment_gateways/{module}.py", {name}, self.namespace)
        gateway = object.__new__(self.namespace[name])
        gateway.session = SessionContext
        gateway.config = NS(
            yoomoney=NS(NOTIFICATION_SECRET="private-key"),
            cryptomus=NS(API_KEY="private-key", MERCHANT_ID="merchant-1"),
            heleket=NS(API_KEY="private-key", MERCHANT_ID="merchant-1"),
            PAYMENT_TRUSTED_PROXY_NETWORKS=[],
        )
        gateway.bot = NS(create_invoice_link=AsyncMock(return_value="sensitive-payment-url"),
                         refund_star_payment=AsyncMock())
        gateway.handle_payment_succeeded = AsyncMock()
        gateway.handle_payment_canceled = AsyncMock()
        self.data.state = gateway.callback
        self.records["order-1"] = NS(
            tg_id=123, subscription="saved-subscription", payment_id="order-1",
            status=TransactionStatus.PENDING,
            **payment_snapshot(provider, gateway.currency.code, 100,
                               None if provider in ("stars", "yoomoney") else "remote-1"),
        )
        return gateway

    def request(self, body, remote="91.227.144.54", headers=None):
        return NS(json=AsyncMock(return_value=body), post=AsyncMock(return_value=body),
                  remote=remote, headers=headers or {})

    def yoomoney_event(self, **changes):
        event = dict(notification_type="card-incoming", operation_id="operation-1", amount="98.00",
                     withdraw_amount="100.00", currency="643", datetime="2026-10-02T12:00:00Z",
                     sender="", codepro="false", unaccepted="false", label="order-1")
        event.update(changes)
        canonical = "&".join(f"{key}={quote(event[key], safe='~-._')}" for key in sorted(event))
        event["sign"] = hmac.new(b"private-key", canonical.encode(), hashlib.sha256).hexdigest()
        return event

    def crypto_event(self, **changes):
        event = dict(type="payment", uuid="remote-1", order_id="order-1", amount="100.00000000",
                     currency="USD", status="paid", is_final=True, additional_data="123")
        event.update(changes)
        raw = json.dumps(event, ensure_ascii=False, separators=(",", ":")).replace("/", "\\/")
        event["sign"] = hashlib.md5(base64.b64encode(raw.encode()) + b"private-key").hexdigest()
        return event

    async def test_yoomoney_signed_amount_order_accepted(self):
        gateway = self.gateway("yoomoney")
        response = await gateway.webhook_handler(self.request(self.yoomoney_event()))
        self.assertEqual(response.status, 200)
        gateway.handle_payment_succeeded.assert_awaited_once_with("order-1")

    async def test_yoomoney_wrong_signature_rejected(self):
        gateway = self.gateway("yoomoney")
        event = self.yoomoney_event()
        event["sign"] = "0" * 64
        self.assertEqual((await gateway.webhook_handler(self.request(event))).status, 403)
        gateway.handle_payment_succeeded.assert_not_awaited()

    async def test_yoomoney_signed_mismatches_rejected(self):
        gateway = self.gateway("yoomoney")
        for changes in ({"withdraw_amount": "1"}, {"currency": "840"}, {"label": "unknown"},
                        {"notification_type": "outgoing"}, {"unaccepted": "true"},
                        {"test_notification": "true"}, {"codepro": "true"}):
            with self.subTest(changes=changes):
                response = await gateway.webhook_handler(self.request(self.yoomoney_event(**changes)))
                self.assertEqual(response.status, 400)
                gateway.handle_payment_succeeded.assert_not_awaited()

    async def test_yoomoney_legacy_sha1_compare_digest_without_provisioning(self):
        gateway = self.gateway("yoomoney")
        event = self.yoomoney_event()
        del event["sign"]
        fields = [event[k] for k in ("notification_type", "operation_id", "amount", "currency", "datetime", "sender", "codepro")]
        event["sha1_hash"] = hashlib.sha1("&".join(fields + ["private-key", event["label"]]).encode()).hexdigest()
        with patch.object(hmac, "compare_digest", wraps=hmac.compare_digest) as comparison:
            self.assertTrue(gateway.verify_notification(event))
            comparison.assert_called_once()
        self.assertEqual((await gateway.webhook_handler(self.request(event))).status, 400)
        gateway.handle_payment_succeeded.assert_not_awaited()

    async def test_yoomoney_documented_hmac_vector(self):
        gateway = self.gateway("yoomoney")
        gateway.config.yoomoney.NOTIFICATION_SECRET = "secret123"
        event = dict(notification_type="p2p-incoming", operation_id="441361714955017004", amount="98.00",
            withdraw_amount="100.00", currency="643", datetime="2013-12-26T08:28:34Z", sender="41000000000",
            codepro="false", label="ML23045", unaccepted="false", sha1_hash="ac13833bd6ba9eff1fa9e4bed76f3d6ebb57f6c0",
            sign="a452af731650e2c5b39abcdc7c28dd27db7b3b654c2230ad2c386e64afb98605")
        self.assertTrue(gateway.verify_notification(event))

    async def test_yoomoney_internal_failure_is_503(self):
        gateway = self.gateway("yoomoney")
        gateway._get_verified_order = AsyncMock(side_effect=RuntimeError("sensitive-provider-payload"))
        self.assertEqual((await gateway.webhook_handler(self.request(self.yoomoney_event()))).status, 503)

    def yookassa_object(self, **changes):
        result = NS(id="order-1", amount=NS(value="100.00", currency="RUB"), status="succeeded",
                    paid=True, metadata={"tg_id": "123", "subscription": "saved-subscription"})
        for key, value in changes.items():
            setattr(result, key, value)
        return result

    async def test_yookassa_authoritative_api_object_accepted(self):
        gateway = self.gateway("yookassa")
        self.records["order-1"].provider_payment_id = "order-1"
        self.namespace["Payment"].find_one.return_value = self.yookassa_object()
        response = await gateway.webhook_handler(self.request(
            {"event": "payment.succeeded", "object": {"id": "order-1", "amount": {"value": "999"}}},
            headers={"X-Forwarded-For": "fake-IP"},
        ))
        self.assertEqual(response.status, 200)
        self.namespace["Payment"].find_one.assert_called_once_with("order-1")
        gateway.handle_payment_succeeded.assert_awaited_once_with("order-1")

    async def test_yookassa_api_mismatches_rejected(self):
        gateway = self.gateway("yookassa")
        self.records["order-1"].provider_payment_id = "order-1"
        objects = [self.yookassa_object(amount=NS(value="1", currency="RUB")),
                   self.yookassa_object(amount=NS(value="100", currency="USD")),
                   self.yookassa_object(status="waiting_for_capture"), self.yookassa_object(paid=False),
                   self.yookassa_object(id="other-order"), self.yookassa_object(metadata={"tg_id": "999"})]
        for obj in objects:
            with self.subTest(object=obj):
                self.namespace["Payment"].find_one.return_value = obj
                response = await gateway.webhook_handler(self.request({"event": "payment.succeeded", "object": {"id": "order-1"}}))
                self.assertEqual(response.status, 400)
                gateway.handle_payment_succeeded.assert_not_awaited()

    async def test_yookassa_unknown_order_no_lookup(self):
        gateway = self.gateway("yookassa")
        response = await gateway.webhook_handler(self.request({"event": "payment.succeeded", "object": {"id": "other"}}))
        self.assertEqual(response.status, 400)
        self.namespace["Payment"].find_one.assert_not_called()

    async def test_yookassa_api_outage_returns_503(self):
        gateway = self.gateway("yookassa")
        self.namespace["Payment"].find_one.side_effect = RuntimeError("secret-api-response")
        response = await gateway.webhook_handler(self.request({"event": "payment.succeeded", "object": {"id": "order-1"}}))
        self.assertEqual(response.status, 503)
        gateway.handle_payment_succeeded.assert_not_awaited()

    async def test_crypto_valid_callbacks(self):
        for provider, ip in (("cryptomus", "91.227.144.54"), ("heleket", "31.133.220.8")):
            with self.subTest(provider=provider):
                gateway = self.gateway(provider)
                event = self.crypto_event()
                response = await gateway.webhook_handler(self.request(event, ip))
                self.assertEqual(response.status, 200)
                gateway.handle_payment_succeeded.assert_awaited_once_with("order-1")
                self.assertIn("sign", event)  # Verification must not mutate provider data.

    async def test_crypto_wrong_signature(self):
        for provider, ip in (("cryptomus", "91.227.144.54"), ("heleket", "31.133.220.8")):
            gateway = self.gateway(provider)
            event = self.crypto_event()
            event["sign"] = "0" * 32
            self.assertEqual((await gateway.webhook_handler(self.request(event, ip))).status, 403)
            gateway.handle_payment_succeeded.assert_not_awaited()

    async def test_crypto_signed_mismatches_rejected(self):
        for provider, ip in (("cryptomus", "91.227.144.54"), ("heleket", "31.133.220.8")):
            gateway = self.gateway(provider)
            for changes in ({"amount": "1"}, {"currency": "RUB"}, {"order_id": "unknown"},
                            {"uuid": "other-invoice"}, {"status": "wrong_amount"},
                            {"is_final": False}, {"type": "wallet"}, {"additional_data": "999"},
                            {"merchant": "other-merchant"}):
                with self.subTest(provider=provider, changes=changes):
                    response = await gateway.webhook_handler(self.request(self.crypto_event(**changes), ip))
                    self.assertEqual(response.status, 400)
                    gateway.handle_payment_succeeded.assert_not_awaited()

    async def test_crypto_header_spoof_cannot_replace_peer(self):
        for provider, ip in (("cryptomus", "91.227.144.54"), ("heleket", "31.133.220.8")):
            gateway = self.gateway(provider)
            response = await gateway.webhook_handler(self.request(self.crypto_event(), "203.0.113.1",
                {"X-Forwarded-For": ip, "CF-Connecting-IP": ip, "X-Real-IP": ip}))
            self.assertEqual(response.status, 403)
            gateway.handle_payment_succeeded.assert_not_awaited()

    async def test_crypto_order_a_cannot_confirm_existing_order_b(self):
        for provider, ip in (("cryptomus", "91.227.144.54"), ("heleket", "31.133.220.8")):
            gateway = self.gateway(provider)
            self.records["order-2"] = NS(tg_id=123, subscription="saved-subscription", payment_id="order-2",
                status=TransactionStatus.PENDING, **payment_snapshot(provider, "USD", "100", "remote-2"))
            response = await gateway.webhook_handler(self.request(self.crypto_event(order_id="order-2"), ip))
            self.assertEqual(response.status, 400)
            gateway.handle_payment_succeeded.assert_not_awaited()

    async def test_crypto_signature_handles_php_slash_unicode_serialization(self):
        for provider, ip in (("cryptomus", "91.227.144.54"), ("heleket", "31.133.220.8")):
            gateway = self.gateway(provider)
            raw = '{"txid":"path\\/é"}'
            event = dict(txid="path/é", sign=hashlib.md5(base64.b64encode(raw.encode()) + b"private-key").hexdigest())
            self.assertTrue(gateway.verify_webhook(self.request(event, ip), event))

    async def test_stars_valid_checkout_and_duplicate_same_order(self):
        gateway = self.gateway("stars")
        await gateway.validate_checkout("order-1", 123, 100, "XTR")
        for _ in range(2):
            await gateway.process_successful_payment("order-1", 123, 100, "XTR", "charge-1")
        self.assertEqual(len(self.records), 1)
        self.assertEqual(self.records["order-1"].provider_payment_id, "charge-1")
        self.assertEqual(gateway.handle_payment_succeeded.await_count, 2)  # Both enter Patch 3's guarded flow.

    async def test_stars_currency_amount_user_payload_charge_mismatch(self):
        gateway = self.gateway("stars")
        for args in (("order-1", 123, 100, "RUB", "charge"), ("order-1", 123, 1, "XTR", "charge"),
                     ("order-1", 999, 100, "XTR", "charge"), ("unknown", 123, 100, "XTR", "charge"),
                     ("order-1", 123, 100, "XTR", "")):
            with self.subTest(args=args), self.assertRaises(InvalidPayment):
                await gateway.process_successful_payment(*args)
        gateway.handle_payment_succeeded.assert_not_awaited()

    async def test_stars_developer_invoice_stores_one_star_and_refunds_once(self):
        gateway = self.gateway("stars")
        self.dev = True
        await gateway.create_payment(self.data)
        invoice = gateway.bot.create_invoice_link.call_args.kwargs
        order_id = invoice["payload"]
        self.assertEqual(invoice["prices"][0].amount, 1)
        self.assertEqual(self.records[order_id].expected_amount, "1")
        await gateway.validate_checkout(order_id, 123, 1, "XTR")
        for _ in range(2):
            await gateway.process_successful_payment(order_id, 123, 1, "XTR", "charge-dev")
        gateway.bot.refund_star_payment.assert_awaited_once()

    async def test_logging_does_not_expose_fields_signatures_or_errors(self):
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        log = self.namespace["logger"]
        log.addHandler(handler)
        try:
            gateway = self.gateway("yoomoney")
            event = self.yoomoney_event(sender="sensitive-sender")
            event["sign"] = "sensitive-signature"
            await gateway.webhook_handler(self.request(event))
            gateway._get_verified_order = AsyncMock(side_effect=RuntimeError("sensitive-api-error"))
            await gateway.webhook_handler(self.request(self.yoomoney_event()))
            for provider, ip in (("cryptomus", "91.227.144.54"), ("heleket", "31.133.220.8")):
                gateway = self.gateway(provider)
                event = self.crypto_event(txid="sensitive-wallet-address")
                event["sign"] = "e" * 32
                await gateway.webhook_handler(self.request(event, ip))
            gateway = self.gateway("yookassa")
            self.namespace["Payment"].find_one.side_effect = RuntimeError("sensitive-provider-response")
            await gateway.webhook_handler(self.request({"event": "payment.succeeded", "object": {"id": "order-1"}}))
            gateway = self.gateway("stars")
            await gateway.create_payment(self.data)
            self.assertNotIn("sensitive", output.getvalue())
            self.assertNotIn("private-key", output.getvalue())
        finally:
            log.removeHandler(handler)

    async def test_startup_sends_secret_to_telegram_without_logging_it(self):
        source = ROOT / "app/__main__.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        function = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "on_startup")
        tasks = NS(transactions=NS(start_scheduler=Mock()), referral=NS(start_scheduler=Mock()),
                   subscription_expiry=NS(start_scheduler=Mock()))
        log = NS(info=Mock())
        namespace = dict(urljoin=urljoin, TELEGRAM_WEBHOOK="/telegram", BOT_STARTED_TAG="started",
                         tasks=tasks, logging=log)
        module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), function], type_ignores=[])
        exec(compile(ast.fix_missing_locations(module), str(source), "exec"), namespace)
        bot = NS(set_webhook=AsyncMock(), get_webhook_info=AsyncMock(return_value=NS(url="https://test.invalid/telegram")))
        config = NS(bot=NS(DOMAIN="https://test.invalid", WEBHOOK_SECRET="private-webhook-secret"),
                    shop=NS(REFERRER_REWARD_ENABLED=False))
        services = NS(notification=NS(notify_developer=AsyncMock()), vpn=object())
        await namespace["on_startup"](config, bot, services, NS(session=object()), object(), object())
        bot.set_webhook.assert_awaited_once_with("https://test.invalid/telegram", secret_token="private-webhook-secret")
        self.assertNotIn("private-webhook-secret", repr(log.info.call_args_list))


class ValidationHelperTests(unittest.TestCase):
    def test_money_normalization_and_invalid_values(self):
        self.assertEqual(money("100"), money("100.00"))
        for value in ("NaN", "Infinity", "-1", "0", None, True):
            with self.assertRaises(InvalidPayment):
                money(value)

    def test_proxy_chain_only_trusts_explicit_hops(self):
        self.assertEqual(safe_client_ip("10.0.0.1", {"X-Forwarded-For": "91.227.144.54"}), "10.0.0.1")
        self.assertEqual(safe_client_ip("10.0.0.1", {"X-Forwarded-For": "evil, 91.227.144.54"}, ["10.0.0.1"]), "91.227.144.54")
        self.assertIsNone(safe_client_ip("10.0.0.1", {"X-Forwarded-For": "invalid"}, ["10.0.0.1"]))

    def test_webhook_secret_configuration_fails_closed(self):
        namespace = dict(re=re)
        load_definitions("app/config.py", {"validate_webhook_secret"}, namespace)
        validate = namespace["validate_webhook_secret"]
        self.assertEqual(validate("test_secret-123"), "test_secret-123")
        for secret in (None, "", "secret with spaces", "x" * 257):
            with self.assertRaises(ValueError):
                validate(secret)


try:
    from aiogram.webhook.aiohttp_server import SimpleRequestHandler
except ImportError:
    SimpleRequestHandler = None


@unittest.skipIf(SimpleRequestHandler is None, "Install aiogram to verify native webhook secret checks")
class NativeTelegramWebhookTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_correct_wrong_missing_secret(self):
        dispatcher = NS(feed_webhook_update=AsyncMock(return_value=None))
        handler = SimpleRequestHandler(dispatcher=dispatcher, bot=NS(session=NS(json_loads=json.loads)),
                                       secret_token="private-webhook-secret", handle_in_background=False)
        for secret in ("", "wrong"):
            request = NS(headers={"X-Telegram-Bot-Api-Secret-Token": secret}, json=AsyncMock(return_value={}))
            response = await handler.handle(request)
            self.assertEqual(response.status, 401)
            dispatcher.feed_webhook_update.assert_not_awaited()
        request = NS(headers={"X-Telegram-Bot-Api-Secret-Token": "private-webhook-secret"}, json=AsyncMock(return_value={}))
        response = await handler.handle(request)
        self.assertEqual(response.status, 200)
        dispatcher.feed_webhook_update.assert_awaited_once()

    async def test_native_secret_not_logged(self):
        dispatcher = NS(feed_webhook_update=AsyncMock(return_value=None))
        handler = SimpleRequestHandler(dispatcher=dispatcher, bot=NS(session=NS(json_loads=json.loads)),
            secret_token="private-webhook-secret", handle_in_background=False)
        with patch.object(logging.Logger, "_log") as logged:
            for value in ("private-webhook-secret", "wrong-secret"):
                await handler.handle(NS(headers={"X-Telegram-Bot-Api-Secret-Token": value}, json=AsyncMock(return_value={})))
            self.assertNotIn("private-webhook-secret", repr(logged.call_args_list))


if __name__ == "__main__":
    unittest.main()
