"""Real Alembic/SQLAlchemy checks against temporary SQLite databases."""

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_safe_inbound import ROOT

try:
    import sqlalchemy as sa
    from alembic import command
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.bot.utils.constants import TransactionStatus
    from app.db.models import Transaction
except ImportError:
    DB_DEPENDENCIES_AVAILABLE = False
else:
    DB_DEPENDENCIES_AVAILABLE = True


PREVIOUS_HEAD = "032f2bef8d8d"
NEW_HEAD = "b6d4e8a72c13"
SECURITY_HEAD = "c4e91b2a70d5"


@unittest.skipUnless(DB_DEPENDENCIES_AVAILABLE, "Install DB dependencies for integration tests")
class MigrationIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db_path = Path(self.directory.name) / "migration.sqlite3"
        self.alembic_config = Config(str(ROOT / "app/db/alembic.ini"))
        self.alembic_config.set_main_option("script_location", str(ROOT / "app/db/migration"))

    def run_migration(self, action, revision):
        db_url = f"sqlite+aiosqlite:///{self.db_path.as_posix()}"
        database = SimpleNamespace(url=lambda: db_url)
        with patch("app.config.load_config", return_value=SimpleNamespace(database=database)):
            action(self.alembic_config, revision)

    def read_statuses(self):
        engine = sa.create_engine(f"sqlite:///{self.db_path.as_posix()}")
        try:
            with sa.orm.Session(engine) as session:
                # Older revisions intentionally do not contain the newer payment snapshot columns.
                return dict(session.execute(sa.select(Transaction.payment_id, Transaction.status)).all())
        finally:
            engine.dispose()

    def test_revision_graph_has_one_head(self):
        script = ScriptDirectory.from_config(self.alembic_config)
        self.assertEqual(script.get_heads(), [SECURITY_HEAD])
        self.assertEqual(script.get_revision(SECURITY_HEAD).down_revision, NEW_HEAD)
        self.assertEqual(script.get_revision(NEW_HEAD).down_revision, PREVIOUS_HEAD)
        self.assertEqual(script.get_revision(PREVIOUS_HEAD).down_revision, "579d48dd94ef")

    def test_security_snapshot_migration_preserves_legacy_and_unique_identity(self):
        self.run_migration(command.upgrade, NEW_HEAD)
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                "INSERT INTO users (tg_id, vpn_id, first_name, language_code, created_at, is_trial_used) "
                "VALUES (123, 'vpn-123', 'Test', 'en', CURRENT_TIMESTAMP, 0)"
            )
            connection.execute(
                "INSERT INTO transactions (tg_id, payment_id, subscription, status, created_at, updated_at) "
                "VALUES (123, 'legacy', 'plan', 'pending', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
            connection.commit()
        self.run_migration(command.upgrade, SECURITY_HEAD)
        engine = sa.create_engine(f"sqlite:///{self.db_path.as_posix()}")
        try:
            with sa.orm.Session(engine) as session:
                inspector = sa.inspect(engine)
                self.assertTrue(any(item["column_names"] == ["payment_id"]
                    for item in inspector.get_unique_constraints("transactions")))
                self.assertTrue(any(item["referred_table"] == "users"
                    for item in inspector.get_foreign_keys("transactions")))
                legacy = session.scalar(sa.select(Transaction))
                self.assertEqual(legacy.payment_id, "legacy")
                self.assertIsNone(legacy.expected_amount)
                session.add(Transaction(tg_id=123, payment_id="new-order", subscription="plan",
                    status=TransactionStatus.PENDING, payment_provider="stars", expected_amount="1",
                    expected_currency="XTR", provider_payment_id="charge-1"))
                session.commit()
                session.add(Transaction(tg_id=123, payment_id="other-order", subscription="plan",
                    status=TransactionStatus.PENDING, payment_provider="stars", expected_amount="1",
                    expected_currency="XTR", provider_payment_id="charge-1"))
                with self.assertRaises(sa.exc.IntegrityError):
                    session.commit()
                session.rollback()
                session.execute(sa.delete(Transaction).where(Transaction.payment_id == "new-order"))
                session.commit()
        finally:
            engine.dispose()
        self.run_migration(command.downgrade, NEW_HEAD)
        self.assertEqual(self.read_statuses(), {"legacy": TransactionStatus.PENDING})

    def test_upgrade_preserves_rows_constraints_and_clean_downgrade(self):
        self.run_migration(command.upgrade, PREVIOUS_HEAD)
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(
                "INSERT INTO users (tg_id, vpn_id, first_name, language_code, "
                "created_at, is_trial_used) VALUES (123, 'vpn-123', 'Test', 'en', "
                "CURRENT_TIMESTAMP, 0)"
            )
            for status in ("pending", "completed", "canceled", "refunded"):
                connection.execute(
                    "INSERT INTO transactions (tg_id, payment_id, subscription, status, "
                    "created_at, updated_at) VALUES (123, ?, 'plan', ?, "
                    "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                    (f"payment-{status}", status),
                )
            connection.commit()

        self.run_migration(command.upgrade, NEW_HEAD)
        statuses = self.read_statuses()
        self.assertEqual(
            statuses,
            {
                "payment-pending": TransactionStatus.PENDING,
                "payment-completed": TransactionStatus.COMPLETED,
                "payment-canceled": TransactionStatus.CANCELED,
                "payment-refunded": TransactionStatus.REFUNDED,
            },
        )
        engine = sa.create_engine(f"sqlite:///{self.db_path.as_posix()}")
        try:
            with engine.connect() as connection:
                inspector = sa.inspect(connection)
                self.assertEqual(
                    {column["name"] for column in inspector.get_columns("transactions")},
                    {"id", "tg_id", "payment_id", "subscription", "status", "created_at", "updated_at"},
                )
                self.assertTrue(any(
                    key["referred_table"] == "users" and key["constrained_columns"] == ["tg_id"]
                    for key in inspector.get_foreign_keys("transactions")
                ))
        finally:
            engine.dispose()

        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO transactions (tg_id, payment_id, subscription, status, "
                    "created_at, updated_at) VALUES (123, 'payment-pending', 'plan', "
                    "'pending', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                )
            connection.rollback()
            for status in ("processing", "review_required"):
                connection.execute(
                    "INSERT INTO transactions (tg_id, payment_id, subscription, status, "
                    "created_at, updated_at) VALUES (123, ?, 'plan', ?, "
                    "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                    (f"payment-{status}", status),
                )
            connection.commit()
        statuses = self.read_statuses()
        self.assertEqual(statuses["payment-processing"], TransactionStatus.PROCESSING)
        self.assertEqual(statuses["payment-review_required"], TransactionStatus.REVIEW_REQUIRED)

        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute("DELETE FROM transactions WHERE status IN ('processing', 'review_required')")
            connection.commit()
        self.run_migration(command.downgrade, PREVIOUS_HEAD)
        self.assertEqual(self.read_statuses(), {
            "payment-pending": TransactionStatus.PENDING,
            "payment-completed": TransactionStatus.COMPLETED,
            "payment-canceled": TransactionStatus.CANCELED,
            "payment-refunded": TransactionStatus.REFUNDED,
        })
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(
                connection.execute("SELECT version_num FROM alembic_version").fetchone()[0],
                PREVIOUS_HEAD,
            )

    def test_downgrade_refuses_unresolved_processing(self):
        self.run_migration(command.upgrade, NEW_HEAD)
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                "INSERT INTO users (tg_id, vpn_id, first_name, language_code, "
                "created_at, is_trial_used) VALUES (123, 'vpn-123', 'Test', 'en', "
                "CURRENT_TIMESTAMP, 0)"
            )
            connection.execute(
                "INSERT INTO transactions (tg_id, payment_id, subscription, status, "
                "created_at, updated_at) VALUES (123, 'needs-review', 'plan', "
                "'processing', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
            connection.commit()
        with self.assertRaisesRegex(RuntimeError, "Resolve processing/review_required"):
            self.run_migration(command.downgrade, PREVIOUS_HEAD)
        self.assertEqual(self.read_statuses()["needs-review"], TransactionStatus.PROCESSING)
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(
                connection.execute("SELECT version_num FROM alembic_version").fetchone()[0],
                NEW_HEAD,
            )


@unittest.skipUnless(DB_DEPENDENCIES_AVAILABLE, "Install DB dependencies for integration tests")
class RealClaimIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_id_binding_is_unique_and_duplicate_does_not_refresh_processing(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = create_async_engine(f"sqlite+aiosqlite:///{(Path(directory) / 'binding.sqlite3').as_posix()}")
            try:
                async with engine.begin() as connection:
                    await connection.run_sync(Transaction.metadata.create_all)
                sessions = async_sessionmaker(engine, expire_on_commit=False)
                async with sessions() as session:
                    await session.execute(sa.text(
                        "INSERT INTO users (tg_id, vpn_id, first_name, language_code, created_at, is_trial_used) "
                        "VALUES (123, 'vpn-123', 'Test', 'en', CURRENT_TIMESTAMP, 0)"
                    ))
                    for order_id in ("one", "two"):
                        session.add(Transaction(tg_id=123, payment_id=order_id, subscription="plan",
                            status=TransactionStatus.PENDING, payment_provider="stars",
                            expected_amount="1", expected_currency="XTR"))
                    await session.commit()
                    self.assertTrue(await Transaction.bind_provider_payment_id(session, "one", "charge-1"))
                    self.assertFalse(await Transaction.bind_provider_payment_id(session, "two", "charge-1"))
                    await session.execute(sa.text(
                        "UPDATE transactions SET status='processing', updated_at='2000-01-01 00:00:00' WHERE payment_id='one'"
                    ))
                    await session.commit()
                async with sessions() as session:
                    self.assertTrue(await Transaction.bind_provider_payment_id(session, "one", "charge-1"))
                    self.assertFalse(await Transaction.bind_provider_payment_id(session, "one", "charge-1", only_if_unbound=True))
                    row = await Transaction.get_by_id(session, "one")
                    self.assertEqual(row.updated_at.year, 2000)
                    self.assertEqual(row.status, TransactionStatus.PROCESSING)
                async with sessions() as stale_session:
                    stale = await Transaction.get_by_id(stale_session, "two")
                    self.assertIsNone(stale.provider_payment_id)
                    async with sessions() as other_session:
                        self.assertTrue(await Transaction.bind_provider_payment_id(other_session, "two", "charge-2"))
                    self.assertFalse(await Transaction.bind_provider_payment_id(stale_session, "two", "wrong-charge"))
            finally:
                await engine.dispose()

    async def test_two_sessions_claim_once(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "claim.sqlite3"
            engine = create_async_engine(f"sqlite+aiosqlite:///{db_path.as_posix()}")
            second_engine = create_async_engine(f"sqlite+aiosqlite:///{db_path.as_posix()}")
            try:
                async with engine.begin() as connection:
                    await connection.run_sync(Transaction.metadata.create_all)
                sessions = async_sessionmaker(engine, expire_on_commit=False)
                second_sessions = async_sessionmaker(second_engine, expire_on_commit=False)
                async with sessions() as session:
                    await session.execute(sa.text(
                        "INSERT INTO users (tg_id, vpn_id, first_name, language_code, "
                        "created_at, is_trial_used) VALUES (123, 'vpn-123', 'Test', 'en', "
                        "CURRENT_TIMESTAMP, 0)"
                    ))
                    session.add(Transaction(
                        tg_id=123, payment_id="payment-1", subscription="plan",
                        status=TransactionStatus.PENDING,
                    ))
                    await session.commit()
                async with sessions() as first_session:
                    first = await Transaction.set_status_if_pending(
                        first_session, "payment-1", TransactionStatus.PROCESSING
                    )
                async with second_sessions() as second_session:
                    second = await Transaction.set_status_if_pending(
                        second_session, "payment-1", TransactionStatus.PROCESSING
                    )
                    transaction = await Transaction.get_by_id(second_session, "payment-1")
                self.assertTrue(first)
                self.assertFalse(second)
                self.assertEqual(transaction.status, TransactionStatus.PROCESSING)
            finally:
                await engine.dispose()
                await second_engine.dispose()


if __name__ == "__main__":
    unittest.main()
