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
                return {row.payment_id: row.status for row in session.scalars(sa.select(Transaction))}
        finally:
            engine.dispose()

    def test_revision_graph_has_one_head(self):
        script = ScriptDirectory.from_config(self.alembic_config)
        self.assertEqual(script.get_heads(), [NEW_HEAD])
        self.assertEqual(script.get_revision(NEW_HEAD).down_revision, PREVIOUS_HEAD)
        self.assertEqual(script.get_revision(PREVIOUS_HEAD).down_revision, "579d48dd94ef")

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
