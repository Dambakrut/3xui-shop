"""Real ORM reservation in independent OS processes; temporary SQLite only."""
import asyncio
import multiprocessing
import tempfile
import unittest
import uuid
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, Transaction, User


def reserve_in_process(path, start, results):
    start.wait(20)

    async def reserve():
        engine = create_async_engine(f"sqlite+aiosqlite:///{Path(path).as_posix()}")
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                row, owner = await Transaction.reserve_purchase_flow(session, "a" * 32,
                    payment_id=str(uuid.uuid4()), tg_id=700, subscription="synthetic-order",
                    payment_provider="stars", expected_currency="XTR", expected_amount="50")
                results.put((owner, row.payment_id, row.purchase_flow_id))
        finally:
            await engine.dispose()
    asyncio.run(reserve())


class PurchaseFlowProcesses(unittest.TestCase):
    def test_two_processes_share_one_orm_reservation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "purchase.sqlite3"
            engine = create_engine(f"sqlite:///{path.as_posix()}")
            try:
                Base.metadata.create_all(engine)
                with Session(engine) as session:
                    session.add(User(tg_id=700, vpn_id=str(uuid.uuid4()), first_name="Offline"))
                    session.commit()
            finally:
                engine.dispose()
            context = multiprocessing.get_context("spawn")
            start, results = context.Event(), context.Queue()
            workers = [context.Process(target=reserve_in_process, args=(str(path), start, results))
                       for _ in range(2)]
            for worker in workers:
                worker.start()
            try:
                start.set()
                outcomes = [results.get(timeout=30) for _ in workers]
                for worker in workers:
                    worker.join(timeout=20)
                    self.assertEqual(worker.exitcode, 0)
            finally:
                for worker in workers:
                    if worker.is_alive():
                        worker.terminate()
                    worker.join(timeout=5)
                results.close()
                results.join_thread()
            self.assertEqual(sorted(r[0] for r in outcomes), [False, True])
            self.assertEqual(outcomes[0][1:], outcomes[1][1:])
            engine = create_engine(f"sqlite:///{path.as_posix()}")
            try:
                with Session(engine) as session:
                    rows = session.scalars(select(Transaction)).all()
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0].purchase_flow_id, "a" * 32)
                    self.assertEqual(rows[0].status.value, "review_required")
            finally:
                engine.dispose()
