"""State transition tests using local SQLite and fakes only."""

import ast
import multiprocessing
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from test_payment_idempotency import Status
from test_safe_inbound import ROOT


def _claim_in_separate_process(db_path, start, results):
    connection = sqlite3.connect(db_path, timeout=10)
    start.wait(10)
    try:
        result = connection.execute(
            "UPDATE transactions SET status='processing' "
            "WHERE payment_id='payment-1' AND status='pending'"
        )
        connection.commit()
        results.put(result.rowcount)
    finally:
        connection.close()


class AtomicClaimTests(unittest.TestCase):
    def test_two_processes_cannot_claim_same_sqlite_transaction(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "payments.db")
            with closing(sqlite3.connect(db_path)) as connection:
                connection.execute(
                    "CREATE TABLE transactions (payment_id TEXT PRIMARY KEY, status TEXT NOT NULL)"
                )
                connection.execute("INSERT INTO transactions VALUES ('payment-1', 'pending')")
                connection.commit()

            context = multiprocessing.get_context("spawn")
            start = context.Event()
            results = context.Queue()
            processes = [
                context.Process(target=_claim_in_separate_process, args=(db_path, start, results))
                for _ in range(2)
            ]
            for process in processes:
                process.start()
            try:
                start.set()
                counts = [results.get(timeout=30) for _ in processes]
                for process in processes:
                    process.join(timeout=30)
                    self.assertEqual(process.exitcode, 0)
            finally:
                for process in processes:
                    if process.is_alive():
                        process.terminate()
                    process.join(timeout=5)
                results.close()
            self.assertEqual(sorted(counts), [0, 1])
            with closing(sqlite3.connect(db_path)) as connection:
                self.assertEqual(
                    connection.execute("SELECT status FROM transactions").fetchone()[0],
                    "processing",
                )

    def test_claim_model_requires_pending_row_and_checks_rowcount(self):
        source = (ROOT / "app/db/models/transaction.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        model = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Transaction")
        method = next(node for node in model.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "set_status_if_pending")
        body = ast.unparse(method)
        self.assertIn("Transaction.status == TransactionStatus.PENDING", body)
        self.assertIn("result.rowcount == 1", body)
        self.assertIn("await session.commit()", body)


class Field:
    def __init__(self, name):
        self.name = name

    def __eq__(self, other):
        return lambda row: getattr(row, self.name) == other

    def __le__(self, other):
        return lambda row: getattr(row, self.name) <= other


class Query:
    def __init__(self, action, field=None):
        self.action = action
        self.field = field
        self.conditions = []
        self.new_values = {}

    def where(self, *conditions):
        self.conditions.extend(conditions)
        return self

    def values(self, **values):
        self.new_values = values
        return self


class FakeResult:
    def __init__(self, count=0, values=None):
        self.rowcount = count
        self._values = values or []

    def scalars(self):
        return self

    def all(self):
        return self._values


class FakeSession:
    def __init__(self, rows):
        self.rows = rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def execute(self, query):
        matches = [row for row in self.rows if all(condition(row) for condition in query.conditions)]
        if query.action == "select":
            return FakeResult(values=[getattr(row, query.field.name) for row in matches])
        for row in matches:
            for name, value in query.new_values.items():
                setattr(row, name, value)
        return FakeResult(count=len(matches))

    async def commit(self):
        pass


class RecoveryTaskTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        self.rows = [
            SimpleNamespace(payment_id="pending-old", status=Status.PENDING,
                            created_at=now - timedelta(hours=2), updated_at=now),
            SimpleNamespace(payment_id="processing-old", status=Status.PROCESSING,
                            created_at=now - timedelta(hours=2), updated_at=now - timedelta(hours=2)),
            SimpleNamespace(payment_id="processing-fresh", status=Status.PROCESSING,
                            created_at=now - timedelta(hours=2), updated_at=now),
            SimpleNamespace(payment_id="completed-old", status=Status.COMPLETED,
                            created_at=now - timedelta(hours=2), updated_at=now - timedelta(hours=2)),
        ]

        class FakeTransaction:
            status = Field("status")
            created_at = Field("created_at")
            updated_at = Field("updated_at")
            payment_id = Field("payment_id")

            @classmethod
            async def set_status_if_processing(cls, session, payment_id, status, updated_before=None):
                row = next(row for row in session.rows if row.payment_id == payment_id)
                if row.status != Status.PROCESSING or row.updated_at > updated_before:
                    return False
                row.status = status
                return True

        self.session = FakeSession(self.rows)
        source = ROOT / "app/bot/tasks/transactions.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        selected = [
            node for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name in {"cancel_expired_transactions", "mark_stale_processing_for_review"}
        ]
        namespace = dict(
            datetime=datetime, timedelta=timedelta, timezone=timezone,
            async_sessionmaker=object,
            select=lambda field: Query("select", field),
            update=lambda model: Query("update"),
            Transaction=FakeTransaction, TransactionStatus=Status,
            logger=SimpleNamespace(info=lambda *args: None, critical=lambda *args: None),
        )
        module = ast.Module(body=selected, type_ignores=[])
        exec(compile(ast.fix_missing_locations(module), str(source), "exec"), namespace)
        self.cancel = namespace["cancel_expired_transactions"]
        self.recover = namespace["mark_stale_processing_for_review"]

    async def test_cancel_only_pending(self):
        await self.cancel(lambda: self.session)
        self.assertEqual([row.status for row in self.rows], [
            Status.CANCELED, Status.PROCESSING, Status.PROCESSING, Status.COMPLETED,
        ])

    async def test_stale_processing_requires_review_without_provisioning(self):
        await self.recover(lambda: self.session)
        self.assertEqual([row.status for row in self.rows], [
            Status.PENDING, Status.REVIEW_REQUIRED, Status.PROCESSING, Status.COMPLETED,
        ])


if __name__ == "__main__":
    unittest.main()
