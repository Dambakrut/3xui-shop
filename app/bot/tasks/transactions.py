import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.utils.constants import TransactionStatus
from app.db.models import Transaction

logger = logging.getLogger(__name__)


async def cancel_expired_transactions(
    session_factory: async_sessionmaker,
    expiration_minutes: int = 15,
) -> None:
    session: AsyncSession
    async with session_factory() as session:
        expiration_time = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=expiration_minutes)
        stmt = update(Transaction).where(
            Transaction.status == TransactionStatus.PENDING,
            Transaction.created_at <= expiration_time,
        ).values(status=TransactionStatus.CANCELED, updated_at=datetime.now(timezone.utc).replace(tzinfo=None))
        result = await session.execute(stmt)
        await session.commit()
        logger.info("[Background check] Canceled %s expired pending transactions.", result.rowcount)


async def mark_stale_processing_for_review(
    session_factory: async_sessionmaker,
    stale_minutes: int = 60,
) -> None:
    # A crashed worker may have applied 3x-ui changes. Never retry these automatically.
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=stale_minutes)
    async with session_factory() as session:
        result = await session.execute(
            select(Transaction.payment_id).where(
                Transaction.status == TransactionStatus.PROCESSING,
                Transaction.updated_at <= cutoff,
            )
        )
        payment_ids = result.scalars().all()
        for payment_id in payment_ids:
            marked = await Transaction.set_status_if_processing(
                session=session,
                payment_id=payment_id,
                status=TransactionStatus.REVIEW_REQUIRED,
                updated_before=cutoff,
            )
            if marked:
                logger.critical(
                    "Payment %s was stale in PROCESSING; manual 3x-ui reconciliation is required.",
                    payment_id,
                )


def start_scheduler(session: async_sessionmaker) -> None:
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        cancel_expired_transactions,
        "interval",
        minutes=15,
        args=[session],
        next_run_time=datetime.now(),
    )
    scheduler.add_job(
        mark_stale_processing_for_review,
        "interval",
        minutes=15,
        args=[session],
        next_run_time=datetime.now(),
    )
    scheduler.start()
