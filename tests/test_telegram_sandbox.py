"""Offline checks of the manually launched live sandbox entrypoint."""
import asyncio
import json
import logging
from pathlib import Path
import tempfile
import unittest
import socket
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from aiogram import Bot
from aiogram.exceptions import TelegramConflictError, TelegramNetworkError
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.methods import GetUpdates, CreateInvoiceLink, SendMessage
from aiogram.types import Update
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from scripts import telegram_sandbox_e2e as sandbox
from app.bot.filters import IsAdmin, IsDev
from app.db.models import Transaction, User
from test_telegram_e2e import RecordingTelegram


TOKEN = "999000111:" + "b" * 35  # Fabricated; never used with a real session.


class SandboxSafety(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sandbox-test-")
        self.old_dev = getattr(IsDev, "developer_id", 0)
        self.old_admins = IsAdmin.admins_ids
        # Even inherited production configuration must not be read.
        with patch("app.config.load_config", side_effect=AssertionError("Production config forbidden")):
            self.path, url = await asyncio.to_thread(sandbox.migrate_sandbox, self.temp.name)
        self.engine = create_async_engine(url)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.sender = RecordingTelegram()
        self.bot = Bot(TOKEN, session=self.sender)
        self.dispatcher, self.gateway, self.gate = sandbox.build_dispatcher(
            self.bot, 700, self.sessions, sandbox.translations(self.temp.name))
        self.sequence = 0
        original_connect = socket.socket.connect
        def local_event_loop_only(sock, address):
            # Windows asyncio creates a loopback socketpair in a worker thread.
            # External destinations remain blocked; all API transports are fake.
            if isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1"):
                return original_connect(sock, address)
            raise AssertionError("External network forbidden")
        self.network = patch("socket.socket.connect", local_event_loop_only)
        self.network.start()

    async def asyncTearDown(self):
        self.network.stop()
        await self.dispatcher.storage.close()
        await self.bot.session.close()
        await self.engine.dispose()
        IsDev.developer_id = self.old_dev
        IsAdmin.admins_ids = self.old_admins
        self.temp.cleanup()

    async def feed(self, kind="message", user_id=700, **fields):
        self.sequence += 1
        user = {"id": user_id, "is_bot": False, "first_name": "Sandbox", "language_code": "en"}
        message = {"message_id": 1, "date": 1, "from": user,
                   "chat": {"id": user_id, "type": "private"}, **fields}
        event = message if kind == "message" else {"id": f"cb{self.sequence}", "from": user,
            "chat_instance": "synthetic", "message": message, **fields}
        return await self.dispatcher.feed_update(self.bot, Update.model_validate(
            {"update_id": self.sequence, kind: event}))

    async def start(self, user_id=700):
        await self.feed(user_id=user_id, text="/start", entities=[
            {"type": "bot_command", "offset": 0, "length": 6}])

    def keyboard(self):
        return next(m.reply_markup for m in reversed(self.sender.calls) if getattr(m, "reply_markup", None))

    async def test_credentials_fail_closed_no_production_fallback(self):
        for env in ({}, {"BOT_TOKEN": TOKEN, "TELEGRAM_E2E_ALLOWED_USER_ID": "700"},
                    {"TELEGRAM_SANDBOX_BOT_TOKEN": TOKEN},
                    {"TELEGRAM_SANDBOX_BOT_TOKEN": TOKEN, "TELEGRAM_E2E_ALLOWED_USER_ID": "-1"},
                    {"TELEGRAM_SANDBOX_BOT_TOKEN": TOKEN, "TELEGRAM_E2E_ALLOWED_USER_ID": "bad"}):
            with self.subTest(keys=list(env)), self.assertRaises(sandbox.SandboxError) as result:
                sandbox.credentials(env)
            self.assertNotIn(TOKEN, str(result.exception))
        self.assertEqual(sandbox.credentials({"TELEGRAM_SANDBOX_BOT_TOKEN": TOKEN,
            "TELEGRAM_E2E_ALLOWED_USER_ID": "700"}), (TOKEN, 700))

    async def test_allowlist_before_db_and_fsm_and_no_response(self):
        with patch.object(sandbox.DBSessionMiddleware, "__call__", AsyncMock(side_effect=AssertionError("DB accessed"))) as db:
            await self.start(701)
            db.assert_not_awaited()
        self.assertEqual(self.dispatcher.storage.storage, {})
        self.assertEqual(self.sender.calls, [])
        async with self.sessions() as session:
            self.assertEqual(list((await session.scalars(select(User))).all()), [])

    async def test_allowed_user_reaches_real_start_and_db(self):
        await self.start()
        self.assertTrue(self.sender.calls)
        async with self.sessions() as session:
            self.assertIsNotNone(await session.scalar(select(User).where(User.tg_id == 700)))
            self.assertEqual(await session.scalar(text("SELECT version_num FROM alembic_version")), "d7a2f6c890e1")

    async def test_real_ui_fake_invoice_duplicate_back_new_flow(self):
        from app.bot.utils.navigation import NavSubscription as Nav
        from app.bot.models import SubscriptionData
        await self.start()
        await self.feed("callback_query", data=Nav.MAIN)
        for _ in range(3):
            await self.feed("callback_query", data=self.keyboard().inline_keyboard[0][0].callback_data)
        checkout = self.keyboard().inline_keyboard[0][0].callback_data
        await self.feed("callback_query", data=checkout)
        action = self.keyboard().inline_keyboard[0][0].url
        await self.feed("callback_query", data=checkout)
        self.assertEqual(self.keyboard().inline_keyboard[0][0].url, action)
        back = self.keyboard().inline_keyboard[1][0].callback_data
        await self.feed("callback_query", data=back)
        await self.feed("callback_query", data=self.keyboard().inline_keyboard[0][0].callback_data)
        self.assertEqual(self.gateway.invoice_calls, 1)
        self.assertEqual(self.gateway.reuse_count, 2)
        self.assertTrue(action.startswith("https://example.test/sandbox/"))
        self.assertFalse(any(m.__api_method__ == "createInvoiceLink" for m in self.sender.calls))
        async with self.sessions() as session:
            rows = list((await session.scalars(select(Transaction))).all())
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].payment_provider, "sandbox")
        await self.feed("callback_query", data=SubscriptionData(state=Nav.PROCESS, user_id=700).pack())
        for _ in range(3):
            await self.feed("callback_query", data=self.keyboard().inline_keyboard[0][0].callback_data)
        async with self.sessions() as session:
            rows = list((await session.scalars(select(Transaction))).all())
            self.assertEqual(len(rows), 2)
            self.assertNotEqual(rows[0].purchase_flow_id, rows[1].purchase_flow_id)
        self.assertEqual(self.gateway.invoice_calls, 2)

    async def test_payment_updates_blocked_before_db(self):
        with patch.object(sandbox.DBSessionMiddleware, "__call__", AsyncMock(side_effect=AssertionError("DB accessed"))) as db:
            await self.feed(successful_payment={"currency": "XTR", "total_amount": 1,
                "invoice_payload": "synthetic-sensitive", "telegram_payment_charge_id": "synthetic-charge",
                "provider_payment_charge_id": ""})
            event = Update.model_validate({"update_id": 77, "pre_checkout_query": {
                "id": "test", "from": {"id": 700, "is_bot": False, "first_name": "Test"},
                "currency": "XTR", "total_amount": 1, "invoice_payload": "synthetic-sensitive"}})
            await self.dispatcher.feed_update(self.bot, event)
            db.assert_not_awaited()
        self.assertEqual(self.sender.calls, [])

    async def test_webhook_present_stops_without_delete_or_get_updates(self):
        bot = NS(get_webhook_info=AsyncMock(return_value=NS(url="https://example.test/webhook")),
                 delete_webhook=AsyncMock(), get_me=AsyncMock(), get_updates=AsyncMock())
        with self.assertRaises(sandbox.SandboxError):
            await sandbox.poll_once_loop(bot, self.dispatcher)
        bot.delete_webhook.assert_not_awaited()
        bot.get_me.assert_not_awaited()
        bot.get_updates.assert_not_awaited()

    async def test_polling_conflict_no_retry(self):
        bot = NS(get_webhook_info=AsyncMock(return_value=NS(url="")),
            get_me=AsyncMock(return_value=NS(id=1, username="synthetic_bot", first_name="Sandbox")),
            get_updates=AsyncMock(side_effect=TelegramConflictError(GetUpdates(), "private-error-token")))
        with patch("builtins.print"), self.assertRaises(sandbox.SandboxError) as result:
            await sandbox.poll_once_loop(bot, self.dispatcher)
        bot.get_updates.assert_awaited_once()
        self.assertNotIn("private-error-token", str(result.exception))

    async def test_transport_rejects_payment_and_other_recipients_before_network(self):
        session = sandbox.TelegramOnlySession(700)
        try:
            for method in (CreateInvoiceLink(title="test", description="test", payload="private",
                                            currency="XTR", prices=[]),
                           SendMessage(chat_id=701, text="private")):
                with self.assertRaises(sandbox.SandboxError):
                    await session.make_request(self.bot, method)
            self.assertEqual(session.requests, 0)
        finally:
            await session.close()

    async def test_no_xui_no_production_config_no_developer_summary_private_fields(self):
        with self.assertRaises(sandbox.SandboxError):
            await sandbox.FakeXUI().sync_servers()
        with self.assertRaises(sandbox.SandboxError):
            await self.gateway.handle_payment_succeeded("synthetic-charge")
        with self.assertRaises(sandbox.SandboxError):
            await self.dispatcher.workflow_data["services"].notification.notify_developer("private")
        await self.test_real_ui_fake_invoice_duplicate_back_new_flow()
        report = await sandbox.summary(self.sessions, self.gateway, NS(requests=0), self.path)
        rendered = json.dumps(report)
        async with self.sessions() as session:
            for row in await session.scalars(select(Transaction)):
                self.assertNotIn(row.payment_url, rendered)
                self.assertNotIn(row.purchase_flow_id, rendered)
        self.assertNotIn(TOKEN, rendered)
        self.assertEqual(report["transaction_count"], 2)

    async def test_polling_transport_error_stops_without_retry(self):
        bot = NS(get_webhook_info=AsyncMock(return_value=NS(url="")),
            get_me=AsyncMock(return_value=NS(id=1, username="synthetic_bot", first_name="Sandbox")),
            get_updates=AsyncMock(side_effect=TimeoutError("synthetic-sensitive-error")))
        with patch("builtins.print"), self.assertRaises(TimeoutError):
            await sandbox.poll_once_loop(bot, self.dispatcher)
        bot.get_updates.assert_awaited_once()

    async def test_runtime_failure_diagnostic_redacts_exception_payload(self):
        failure = RuntimeError("synthetic-sensitive-error " + TOKEN)
        with patch.object(sandbox, "run", AsyncMock(side_effect=failure)), patch("builtins.print") as output:
            # main() owns asyncio.run, so invoke in an independent worker.
            result = await asyncio.to_thread(sandbox.main)
        self.assertEqual(result, 1)
        rendered = repr(output.call_args_list)
        self.assertIn("RuntimeError", rendered)
        self.assertNotIn(TOKEN, rendered)
        self.assertNotIn("synthetic-sensitive-error", rendered)

    async def test_production_environment_does_not_select_database_or_redis(self):
        with tempfile.TemporaryDirectory(prefix="sandbox-env-test-") as directory:
            with patch.dict("os.environ", {"DB_DATA_DIR": "PRODUCTION_MUST_NOT_BE_USED",
                    "DB_NAME": "production", "REDIS_HOST": "production.invalid",
                    "BOT_TOKEN": "invalid-production-token"}), \
                    patch("redis.asyncio.Redis.from_url", side_effect=AssertionError("Redis forbidden")):
                path, url = await asyncio.to_thread(sandbox.migrate_sandbox, directory)
                self.assertEqual(path.parent, Path(directory))
                self.assertNotIn("production", url)
                self.assertTrue(path.exists())

    async def test_owned_runner_cleanup_and_banner_without_network_or_secret(self):
        previous_logging = logging.root.manager.disable
        try:
            with patch.dict("os.environ", {"TELEGRAM_SANDBOX_BOT_TOKEN": TOKEN,
                    "TELEGRAM_E2E_ALLOWED_USER_ID": "700"}), \
                    patch.object(sandbox, "poll_once_loop", AsyncMock()) as polling, \
                    patch.object(sandbox.TelegramOnlySession, "make_request", AsyncMock(side_effect=AssertionError("Live API forbidden"))) as wire, \
                    patch("builtins.print") as output:
                await sandbox.run()
            wire.assert_not_awaited()
            polling.assert_awaited_once()
            rendered = repr(output.call_args_list)
            self.assertIn("TELEGRAM SANDBOX E2E MODE", rendered)
            self.assertNotIn(TOKEN, rendered)
            report = next(json.loads(c.args[0]) for c in output.call_args_list
                          if c.args and str(c.args[0]).startswith('{"transaction_count"'))
            self.assertEqual(report["telegram_request_attempts"], 0)
            self.assertFalse(Path(report["sandbox_db"]).exists())
        finally:
            logging.disable(previous_logging)

    async def test_sandbox_trust_env_and_production_session_unchanged(self):
        session = sandbox.TelegramOnlySession(700)
        production_default = AiohttpSession()
        try:
            http = await session.create_session()
            self.assertTrue(http.trust_env)
            self.assertIs(await session.create_session(), http)
            self.assertTrue(http.connector._ssl)  # TLS verification not disabled.
            await session.close()
            self.assertTrue(http.closed)
            replacement = await session.create_session()
            self.assertTrue(replacement.trust_env)
            self.assertIsNot(replacement, http)
            self.assertFalse((await production_default.create_session()).trust_env)
            self.assertEqual(session.requests, 0)
        finally:
            await session.close()
            await production_default.close()

    async def test_sandbox_source_no_hardcoded_proxy(self):
        source = Path(sandbox.__file__).read_text(encoding="utf-8")
        for forbidden in ("127.0.0.1", "10809", "HTTP_PROXY=", "HTTPS_PROXY=", "socks://"):
            self.assertNotIn(forbidden, source)

    async def test_network_diagnostics_categories_no_sensitive_details(self):
        private = TOKEN + " https://user:private-proxy-password@proxy.invalid/bot/" + TOKEN
        for hint, expected in (("Request timeout error", "timeout"),
                               ("ClientConnectorError", "connector"),
                               ("ClientConnectorDNSError", "DNS"),
                               ("ClientConnectorSSLError", "SSL"),
                               ("unspecified", "unknown")):
            with self.subTest(category=expected):
                failure = TelegramNetworkError(GetUpdates(), hint + " " + private)
                with patch.object(sandbox, "run", AsyncMock(side_effect=failure)), patch("builtins.print") as output:
                    self.assertEqual(await asyncio.to_thread(sandbox.main), 1)
                rendered = repr(output.call_args_list)
                self.assertIn("category=" + expected, rendered)
                self.assertIn("host=api.telegram.org", rendered)
                for forbidden in (TOKEN, "private-proxy-password", "proxy.invalid", "/bot/"):
                    self.assertNotIn(forbidden, rendered)

    async def prepare_fake_payment(self):
        from app.bot.utils.navigation import NavSubscription as Nav
        await self.start()
        await self.feed("callback_query", data=Nav.MAIN)
        for _ in range(3):
            await self.feed("callback_query", data=self.keyboard().inline_keyboard[0][0].callback_data)
        checkout = self.keyboard().inline_keyboard[0][0].callback_data
        await self.feed("callback_query", data=checkout)
        return checkout

    async def test_duplicate_callback_telemetry_reused_no_invoice(self):
        checkout = await self.prepare_fake_payment()
        created = self.gateway.telemetry.events[-1]
        self.assertEqual(created["event"], "payment_method_selected")
        self.assertTrue(created["created"])
        self.assertEqual(created["invoice_delta"], 1)
        self.assertTrue(all(result == "FAIL" for result in self.gateway.telemetry.assertions().values()))
        await self.feed("callback_query", data=checkout)
        event = self.gateway.telemetry.events[-1]
        self.assertEqual(event["event"], "payment_reused")
        self.assertEqual(event["flow"], created["flow"])
        self.assertEqual(event["active_flow_before"], event["active_flow_after"])
        self.assertFalse(event["created"])
        self.assertTrue(event["reused"])
        self.assertEqual(event["invoice_delta"], 0)
        self.assertEqual(event["transaction_count"], 1)
        self.assertEqual(event["reuse_count"], 1)
        self.assertEqual(self.gateway.telemetry.assertions()["duplicate_reuse_observed"], "PASS")
        self.assertEqual(self.gateway.telemetry.assertions()["back_reuse_observed"], "FAIL")

    async def test_back_reopen_telemetry_same_flow_and_reuse(self):
        await self.prepare_fake_payment()
        original = self.gateway.telemetry.events[-1]["flow"]
        back = self.keyboard().inline_keyboard[1][0].callback_data
        await self.feed("callback_query", data=back)
        event = self.gateway.telemetry.events[-1]
        self.assertEqual(event["event"], "back_navigation")
        self.assertEqual(event["active_flow_before"], original)
        self.assertEqual(event["active_flow_after"], original)
        self.assertTrue(event["flow_unchanged"])
        await self.feed("callback_query", data=self.keyboard().inline_keyboard[0][0].callback_data)
        reused = self.gateway.telemetry.events[-1]
        self.assertTrue(reused["back_reuse"])
        self.assertEqual(reused["flow"], original)
        self.assertEqual(reused["invoice_delta"], 0)
        self.assertEqual(self.gateway.telemetry.assertions()["back_reuse_observed"], "PASS")

    async def test_new_intentional_flow_telemetry_different_identity(self):
        from app.bot.utils.navigation import NavSubscription as Nav
        from app.bot.models import SubscriptionData
        await self.prepare_fake_payment()
        original = self.gateway.telemetry.events[-1]["flow"]
        await self.feed("callback_query", data=SubscriptionData(state=Nav.PROCESS, user_id=700).pack())
        event = self.gateway.telemetry.events[-1]
        self.assertEqual(event["event"], "intentional_new_checkout")
        self.assertEqual(event["active_flow_before"], original)
        self.assertNotEqual(event["active_flow_after"], original)
        self.assertTrue(event["flows_differ"])
        self.assertEqual(event["invoice_delta"], 0)
        self.assertEqual(self.gateway.telemetry.assertions()["intentional_new_checkout_observed"], "FAIL")
        for _ in range(3):
            await self.feed("callback_query", data=self.keyboard().inline_keyboard[0][0].callback_data)
        event = self.gateway.telemetry.events[-1]
        self.assertTrue(event["created"])
        self.assertEqual(event["transaction_count"], 2)
        self.assertEqual(event["invoice_delta"], 1)
        self.assertEqual(self.gateway.telemetry.assertions()["intentional_new_checkout_observed"], "PASS")

    async def test_blocked_updates_count_and_telemetry_privacy(self):
        await self.start(701)
        await self.feed(user_id=701, text="synthetic-private-message")
        await self.feed("callback_query", user_id=701, data="synthetic-private-callback")
        await self.prepare_fake_payment()
        telemetry = self.gateway.telemetry
        report = await sandbox.summary(self.sessions, self.gateway, NS(requests=0), self.path)
        self.assertEqual(report["blocked_update_count"], 3)
        self.assertEqual(len(telemetry.events), len(report["event_timeline"]))
        with patch("builtins.print") as output:
            sandbox.print_timeline(report)
        rendered = json.dumps(report) + repr(output.call_args_list)
        self.assertIn("## Event timeline", rendered)
        for private in (TOKEN, "synthetic-private-message", "synthetic-private-callback"):
            self.assertNotIn(private, rendered)
        async with self.sessions() as session:
            for row in await session.scalars(select(Transaction)):
                self.assertNotIn(row.purchase_flow_id, rendered)
                self.assertNotIn(row.payment_url, rendered)
        for forbidden in ("callback_data", "message_text", "invoice_payload", "successful_payment"):
            self.assertNotIn(forbidden, rendered)
        self.assertEqual([e["seq"] for e in telemetry.events], list(range(1, len(telemetry.events) + 1)))
