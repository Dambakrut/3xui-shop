"""Real imports and runtime wiring. External network boundaries are blocked/mocked."""
import asyncio
import importlib.util
import os
from pathlib import Path
import socket
import shutil
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
AVAILABLE = all(importlib.util.find_spec(name) for name in
                ("aiogram", "py3xui", "apscheduler", "babel", "sqlalchemy", "alembic", "redis"))


def dummy_env(directory):
    # No production .env or inherited credentials are used.
    return {
        "BOT_TOKEN": "123456789:" + "a" * 35,
        "BOT_WEBHOOK_SECRET": "local_test_secret",
        "BOT_DEV_ID": "123", "BOT_SUPPORT_ID": "123", "BOT_DOMAIN": "shop.invalid",
        "XUI_USERNAME": "dummy", "XUI_PASSWORD": "dummy", "XUI_INBOUND_ID": "42",
        "DB_DATA_DIR": str(directory), "DB_NAME": "runtime",
        "REDIS_HOST": "127.0.0.1", "REDIS_PORT": "1",
        "SHOP_PAYMENT_STARS_ENABLED": "true",
        **{f"SHOP_PAYMENT_{name}_ENABLED": "false" for name in
           ("YOOMONEY", "YOOKASSA", "CRYPTOMUS", "HELEKET")},
    }


def child_env(directory):
    # Retain only OS essentials needed by Windows/subprocess, not application settings.
    env = {key: value for key, value in os.environ.items() if key.upper() in
           ("SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "COMSPEC")}
    env.update(dummy_env(directory))
    env["PYTHONPATH"] = str(ROOT)
    return env


@unittest.skipUnless(AVAILABLE, "Install the locked runtime dependencies with Poetry")
class RuntimeTests(unittest.TestCase):
    def test_each_application_module_imports_in_fresh_process_without_network(self):
        modules = ("app.config", "app.db", "app.db.models", "app.bot.payment_gateways",
                   "app.bot.routers", "app.bot.tasks", "app.bot.services", "app.__main__")
        with tempfile.TemporaryDirectory() as directory:
            for module in modules:
                with self.subTest(module=module):
                    code = (
                        "from unittest.mock import patch; import importlib; "
                        "p=patch('socket.socket.connect', side_effect=AssertionError('network forbidden')); "
                        f"p.start(); importlib.import_module({module!r})"
                    )
                    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                                            env=child_env(directory), capture_output=True,
                                            text=True, timeout=30)
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_stars_only_config_ignores_disabled_credentials(self):
        from app.config import load_config
        from aiogram import Bot
        from aiogram.utils.token import TokenValidationError
        from sqlalchemy.engine import make_url
        with tempfile.TemporaryDirectory() as directory:
            env = dummy_env(directory)
            env.update(YOOKASSA_SHOP_ID="not-an-integer", CRYPTOMUS_API_KEY="",
                       HELEKET_API_KEY="", YOOMONEY_NOTIFICATION_SECRET="")
            with patch.dict(os.environ, env, clear=True), patch("environs.Env.read_env"):
                config = load_config()
            self.assertTrue(config.shop.PAYMENT_STARS_ENABLED)
            self.assertIsNone(config.yookassa.SHOP_ID)
            self.assertIsNone(config.yoomoney.NOTIFICATION_SECRET)
            self.assertIsNone(config.cryptomus.API_KEY)
            self.assertIsNone(config.heleket.API_KEY)
            self.assertEqual(config.xui.INBOUND_ID, 42)
            self.assertEqual(Path(make_url(config.database.url()).database),
                             Path(directory) / "runtime.sqlite3")
            bot = Bot(config.bot.TOKEN)
            asyncio.run(bot.session.close())
            with self.assertRaises(TokenValidationError):
                Bot("malformed-token")
            for missing in ("BOT_WEBHOOK_SECRET", "XUI_INBOUND_ID"):
                env.pop(missing)
                with patch.dict(os.environ, env, clear=True), patch("environs.Env.read_env"):
                    with self.assertRaises(ValueError):
                        load_config()
                env = dummy_env(directory)

    def test_sdk_surface_and_py3xui_imports(self):
        import inspect
        from aiogram import Bot
        from aiogram.types import SuccessfulPayment, PreCheckoutQuery
        from aiogram.webhook.aiohttp_server import SimpleRequestHandler
        from py3xui import AsyncApi, Client, Inbound
        from yookassa import Payment
        import aiohttp
        import requests
        self.assertIn("secret_token", inspect.signature(Bot.set_webhook).parameters)
        self.assertIn("secret_token", inspect.signature(SimpleRequestHandler).parameters)
        self.assertIn("telegram_payment_charge_id", SuccessfulPayment.model_fields)
        self.assertIn("currency", PreCheckoutQuery.model_fields)
        self.assertTrue(callable(Payment.find_one) and callable(Payment.create))
        api = AsyncApi("https://panel.invalid", "dummy", "dummy")
        for owner, method in ((api, "login"), (api.inbound, "get_list"),
                              (api.client, "get_by_email"), (api.client, "add"),
                              (api.client, "update")):
            self.assertTrue(inspect.iscoroutinefunction(getattr(owner, method)))
        self.assertIn("inbound_id", inspect.signature(api.client.add).parameters)
        self.assertIn("client_uuid", inspect.signature(api.client.update).parameters)
        self.assertTrue(Client and Inbound and aiohttp.ClientSession and requests.post)

    def test_real_alembic_cli_full_chain_and_orm(self):
        from sqlalchemy import create_engine, inspect, select
        from sqlalchemy.orm import Session
        from app.db.models import Transaction, User
        from app.bot.utils.constants import TransactionStatus
        with tempfile.TemporaryDirectory() as directory:
            env = child_env(directory)
            for args in (("heads",), ("history",), ("upgrade", "head")):
                result = subprocess.run([sys.executable, "-m", "alembic", "-c",
                                         "app/db/alembic.ini", *args], cwd=ROOT, env=env,
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                if args == ("heads",):
                    self.assertEqual(result.stdout.strip(), "c4e91b2a70d5 (head)")
            engine = create_engine(f"sqlite:///{Path(directory).as_posix()}/runtime.sqlite3")
            try:
                with Session(engine) as session:
                    from sqlalchemy import text
                    session.execute(text("PRAGMA foreign_keys=ON"))
                    session.add(User(tg_id=123, vpn_id="runtime-user", first_name="Test"))
                    session.commit()
                    for status in TransactionStatus:
                        session.add(Transaction(tg_id=123, payment_id=status.value,
                            subscription="saved-order", status=status, payment_provider="stars",
                            expected_amount="1", expected_currency="XTR"))
                    session.commit()
                    rows = session.scalars(select(Transaction)).all()
                    self.assertEqual({row.status for row in rows}, set(TransactionStatus))
                    self.assertTrue(all(row.expected_amount == "1" for row in rows))
                    schema = inspect(engine)
                    self.assertTrue(schema.get_foreign_keys("transactions"))
                    uniques = {tuple(item["column_names"]) for item in
                               schema.get_unique_constraints("transactions")}
                    self.assertIn(("payment_id",), uniques)
                    self.assertIn(("payment_provider", "provider_payment_id"), uniques)
                    self.assertEqual(session.execute(text("PRAGMA foreign_key_check")).all(), [])
                    # Remove test purchases before a downgrade discards snapshot/state support.
                    for row in rows:
                        session.delete(row)
                    session.commit()
            finally:
                engine.dispose()
            result = subprocess.run([sys.executable, "-m", "alembic", "-c",
                                     "app/db/alembic.ini", "downgrade", "base"],
                                    cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)


@unittest.skipUnless(AVAILABLE, "Install the locked runtime dependencies with Poetry")
class AsyncRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_redis_storage_client_and_bounded_connection_failure(self):
        from aiogram.fsm.storage.redis import RedisStorage
        from app.config import RedisConfig
        from redis.exceptions import ConnectionError
        config = RedisConfig("127.0.0.1", 1, "0", None, "p@ss:/word")
        storage = RedisStorage.from_url(config.url(), connection_kwargs={
            "socket_connect_timeout": 0.1, "socket_timeout": 0.1})
        self.assertEqual(storage.redis.connection_pool.connection_kwargs["password"], "p@ss:/word")
        # Force failure at the socket boundary: no Redis server or real connection is used.
        with patch("asyncio.open_connection", side_effect=OSError("local test refusal")):
            with self.assertRaises(ConnectionError):
                await storage.redis.ping()
        await storage.close()

    async def test_real_main_stars_only_and_startup_with_external_calls_mocked(self):
        from contextlib import ExitStack
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from aiogram import Bot
        from py3xui import AsyncApi
        from app.integrations.xui import XUIAdapter
        from app import __main__ as entry
        from app.config import load_config
        from app.db.database import Database
        from app.bot.services import plan
        from app.bot.payment_gateways import TelegramStars
        from app.db.models import Server

        schedulers, databases, captured = [], [], {}
        original_start = AsyncIOScheduler.start

        def paused_start(scheduler, *args, **kwargs):
            schedulers.append(scheduler)
            original_start(scheduler, paused=True)

        class TrackedDatabase(Database):
            def __init__(self, config):
                super().__init__(config)
                databases.append(self)

        async def no_server_run(app, **kwargs):
            app.freeze()
            await app.startup()
            # The actual Dispatcher, services, middleware, gateway and routes exist.
            handler = next(route.handler.__self__ for route in app.router.routes()
                           if getattr(route.handler, "__self__", None)
                           and hasattr(route.handler.__self__, "dispatcher"))
            dp = handler.dispatcher
            captured["storage"] = dp.storage
            gateways = dp["gateway_factory"].get_gateways()
            self.assertEqual(len(gateways), 1)
            self.assertIsInstance(gateways[0], TelegramStars)
            self.assertEqual(handler.secret_token, "local_test_secret")
            self.assertGreater(len(dp.sub_routers), 5)
            self.assertGreaterEqual(sum(len(s.get_jobs()) for s in schedulers), 3)
            await app.shutdown()
            await app.cleanup()

        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            locales = Path(directory) / "locales"
            shutil.copytree(ROOT / "app/locales", locales, ignore=shutil.ignore_patterns("*.mo"))
            compiled = subprocess.run([sys.executable, "-m", "babel.messages.frontend",
                                      "compile", "-d", str(locales), "-D", "bot"],
                                     capture_output=True, text=True, timeout=30)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            stack.enter_context(patch.object(entry, "DEFAULT_LOCALES_DIR", locales))
            stack.enter_context(patch.dict(os.environ, dummy_env(directory), clear=True))
            stack.enter_context(patch("environs.Env.read_env"))
            config = load_config()
            # Real DB and an isolated configured server; panel responses are mocked.
            db = Database(config.database)
            await db.initialize()
            async with db.session() as session:
                session.add(Server(name="test", host="https://panel.invalid", max_clients=10))
                await session.commit()
            await db.close()
            stack.enter_context(patch.object(plan, "DEFAULT_PLANS_DIR", ROOT / "plans.example.json"))
            stack.enter_context(patch.object(entry.logger, "setup_logging"))
            stack.enter_context(patch.object(entry, "Database", TrackedDatabase))
            stack.enter_context(patch.object(entry, "_run_app", no_server_run))
            stack.enter_context(patch.object(AsyncIOScheduler, "start", paused_start))
            login = stack.enter_context(patch.object(AsyncApi, "login", new_callable=AsyncMock))
            adapter_login = stack.enter_context(patch.object(XUIAdapter, "authenticate", new_callable=AsyncMock))
            stack.enter_context(patch.object(XUIAdapter, "get_server_status", new=AsyncMock(
                return_value=SimpleNamespace(panel_version="v3.8.5", xray_state="running"))))
            stack.enter_context(patch.object(XUIAdapter, "list_inbounds", new=AsyncMock(
                return_value=[SimpleNamespace(id=42, enable=True, protocol="vless", remark="Shop")])) )
            adapter_close = stack.enter_context(patch.object(XUIAdapter, "close", new_callable=AsyncMock))
            webhook = stack.enter_context(patch.object(Bot, "set_webhook", new_callable=AsyncMock))
            for method in ("set_my_commands", "delete_my_commands", "delete_webhook", "send_message"):
                stack.enter_context(patch.object(Bot, method, new_callable=AsyncMock))
            stack.enter_context(patch.object(Bot, "get_webhook_info", new=AsyncMock(
                return_value=SimpleNamespace(url="https://shop.invalid/telegram"))))
            # Catch any unexpected network call, including a provider SDK.
            stack.enter_context(patch.object(socket.socket, "connect",
                               side_effect=AssertionError("unexpected external network")))
            try:
                await entry.main()
                adapter_login.assert_awaited_once()
                adapter_close.assert_awaited()
                login.assert_not_awaited()
                self.assertEqual(webhook.await_args.kwargs["secret_token"], "local_test_secret")
            finally:
                for scheduler in schedulers:
                    if scheduler.running:
                        scheduler.shutdown(wait=False)
                await asyncio.sleep(0)
                if "storage" in captured:
                    await captured["storage"].close()
                for database in databases:
                    await database.close()


if __name__ == "__main__":
    unittest.main()
