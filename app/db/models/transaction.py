import logging
from datetime import datetime
from typing import Any, Self

from sqlalchemy import *
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column, relationship, selectinload
from sqlalchemy.types import Enum

from app.bot.utils.constants import TransactionStatus

from . import Base

logger = logging.getLogger(__name__)


class Transaction(Base):
    """
    Represents a transaction in the database.

    Attributes:
        id (int): Unique identifier for the transaction (primary key).
        tg_id (int): Telegram user ID associated with the transaction.
        payment_id (str): Unique payment identifier for the transaction.
        subscription (str): Name of the subscription plan associated with the transaction.
        status (TransactionStatus): Current status of the transaction (e.g., pending, completed).
        created_at (datetime): Timestamp when the transaction was created.
        updated_at (datetime): Timestamp when the transaction was last updated.
        user (User): Related user object.
    """

    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(ForeignKey("users.tg_id"), nullable=False)
    payment_id: Mapped[str] = mapped_column(String(length=64), unique=True, nullable=False)
    subscription: Mapped[str] = mapped_column(String(length=255), nullable=False)
    payment_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    expected_amount: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expected_currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    provider_payment_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    __table_args__ = (
        UniqueConstraint("payment_provider", "provider_payment_id", name="uq_provider_payment"),
    )
    status: Mapped[TransactionStatus] = mapped_column(
        Enum(TransactionStatus, values_callable=lambda obj: [e.value for e in obj]),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    user: Mapped["User"] = relationship("User", back_populates="transactions")  # type: ignore

    def __repr__(self) -> str:
        return (
            f"<Transaction(id={self.id}, tg_id={self.tg_id}, payment_id='{self.payment_id}', "
            f"subscription='{self.subscription}', status='{self.status}', "
            f"created_at={self.created_at}, updated_at={self.updated_at})>"
        )

    @classmethod
    async def get_by_id(cls, session: AsyncSession, payment_id: str) -> Self | None:
        filter = [Transaction.payment_id == payment_id]
        query = await session.execute(
            select(Transaction).options(selectinload(Transaction.user)).where(*filter)
        )
        return query.scalar_one_or_none()

    @classmethod
    async def get_by_user(cls, session: AsyncSession, tg_id: int) -> list[Self]:
        filter = [Transaction.tg_id == tg_id]
        query = await session.execute(
            select(Transaction).options(selectinload(Transaction.user)).where(*filter)
        )
        return query.scalars().all()

    @classmethod
    async def create(cls, session: AsyncSession, payment_id: str, **kwargs: Any) -> Self | None:
        transaction = await Transaction.get_by_id(session=session, payment_id=payment_id)

        if transaction:
            logger.warning(f"Transaction {payment_id} already exists.")
            return None

        transaction = Transaction(payment_id=payment_id, **kwargs)
        session.add(transaction)

        try:
            await session.commit()
            logger.info(f"Transaction {payment_id} created.")
            return transaction
        except IntegrityError as exception:
            await session.rollback()
            logger.error("Could not create transaction (integrity conflict).")
            return None

    @classmethod
    async def bind_provider_payment_id(
        cls, session: AsyncSession, payment_id: str, provider_id: str, only_if_unbound: bool = False
    ) -> bool:
        if not isinstance(provider_id, str) or not provider_id or len(provider_id) > 128:
            return False
        try:
            result = await session.execute(
                update(cls).where(
                    cls.payment_id == payment_id, cls.provider_payment_id.is_(None),
                ).values(provider_payment_id=provider_id).execution_options(synchronize_session=False)
            )
            await session.commit()
            if result.rowcount == 1:
                return True
            if only_if_unbound:
                return False
            bound_id = await session.scalar(select(cls.provider_payment_id).where(cls.payment_id == payment_id))
            return bound_id == provider_id
        except IntegrityError:
            await session.rollback()
            return False

    @classmethod
    async def update(cls, session: AsyncSession, payment_id: str, **kwargs: Any) -> Self | None:
        transaction = await Transaction.get_by_id(session=session, payment_id=payment_id)

        if transaction:
            filter = [Transaction.id == transaction.id]
            await session.execute(update(Transaction).where(*filter).values(**kwargs))
            await session.commit()
            logger.info(f"Transaction {payment_id} updated.")
            return transaction

        logger.warning(f"Transaction {payment_id} not found for update.")
        return None

    @classmethod
    async def set_status_if_pending(
        cls, session: AsyncSession, payment_id: str, status: TransactionStatus
    ) -> bool:
        if status not in (TransactionStatus.PROCESSING, TransactionStatus.CANCELED):
            raise ValueError(f"Invalid transition from pending to {status}")
        result = await session.execute(
            update(Transaction)
            .where(
                Transaction.payment_id == payment_id,
                Transaction.status == TransactionStatus.PENDING,
            )
            .values(status=status, updated_at=func.now())
        )
        await session.commit()
        return result.rowcount == 1

    @classmethod
    async def set_status_if_processing(
        cls,
        session: AsyncSession,
        payment_id: str,
        status: TransactionStatus,
        updated_before: datetime | None = None,
    ) -> bool:
        if status not in (TransactionStatus.COMPLETED, TransactionStatus.REVIEW_REQUIRED):
            raise ValueError(f"Invalid transition from processing to {status}")
        conditions = [
            Transaction.payment_id == payment_id,
            Transaction.status == TransactionStatus.PROCESSING,
        ]
        if updated_before is not None:
            conditions.append(Transaction.updated_at <= updated_before)
        result = await session.execute(
            update(Transaction).where(*conditions).values(status=status, updated_at=func.now())
        )
        await session.commit()
        return result.rowcount == 1
