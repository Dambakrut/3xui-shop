import asyncio
import logging
from abc import ABC, abstractmethod
from weakref import WeakValueDictionary

from aiogram import Bot
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.utils.i18n import I18n
from aiogram.utils.i18n import gettext as _
from aiogram.utils.i18n import lazy_gettext as __
from aiohttp.web import Application
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.bot.models import ServicesContainer, SubscriptionData
from app.bot.routers.main_menu.handler import redirect_to_main_menu
from app.bot.utils.constants import (
    DEFAULT_LANGUAGE,
    EVENT_PAYMENT_CANCELED_TAG,
    EVENT_PAYMENT_SUCCEEDED_TAG,
    Currency,
    TransactionStatus,
)
from app.bot.utils.formatting import format_device_count, format_subscription_period
from app.config import Config
from app.db.models import Transaction, User

logger = logging.getLogger(__name__)
_payment_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


def _payment_lock(payment_id: str) -> asyncio.Lock:
    lock = _payment_locks.get(payment_id)
    if lock is None:
        lock = asyncio.Lock()
        _payment_locks[payment_id] = lock
    return lock

from app.bot.models import SubscriptionData
from app.bot.utils.constants import Currency


class PaymentGateway(ABC):
    name: str
    currency: Currency
    callback: str

    def __init__(
        self,
        app: Application,
        config: Config,
        session: async_sessionmaker,
        storage: RedisStorage,
        bot: Bot,
        i18n: I18n,
        services: ServicesContainer,
    ) -> None:
        self.app = app
        self.config = config
        self.session = session
        self.storage = storage
        self.bot = bot
        self.i18n = i18n
        self.services = services

    @abstractmethod
    async def create_payment(self, data: SubscriptionData) -> str:
        pass

    @abstractmethod
    async def handle_payment_succeeded(self, payment_id: str) -> None:
        pass

    @abstractmethod
    async def handle_payment_canceled(self, payment_id: str) -> None:
        pass

    async def _on_payment_succeeded(self, payment_id: str) -> None:
        logger.info(f"Payment succeeded {payment_id}")

        if not payment_id:
            logger.warning("Payment success callback has no payment ID.")
            return

        async with _payment_lock(payment_id):
            async with self.session() as session:
                transaction = await Transaction.get_by_id(session=session, payment_id=payment_id)
                if not transaction:
                    logger.warning(f"Payment {payment_id} has no matching transaction.")
                    return
                if transaction.status != TransactionStatus.PENDING:
                    logger.info(f"Payment {payment_id} already has status {transaction.status}; ignored.")
                    return
                data = SubscriptionData.unpack(transaction.subscription)
                if data.user_id != transaction.tg_id:
                    logger.error(f"Payment {payment_id} has mismatched user data; ignored.")
                    return
                user = await User.get(session=session, tg_id=transaction.tg_id)
                if not user:
                    logger.error(f"Payment {payment_id} has no matching user; ignored.")
                    return

            if data.is_extend:
                provisioned = await self.services.vpn.extend_subscription(
                    user=user, devices=data.devices, duration=data.duration
                )
            elif data.is_change:
                provisioned = await self.services.vpn.change_subscription(
                    user=user, devices=data.devices, duration=data.duration
                )
            else:
                provisioned = await self.services.vpn.create_subscription(
                    user=user, devices=data.devices, duration=data.duration
                )

            if not provisioned:
                logger.error(f"VPN provisioning failed for payment {payment_id}; transaction remains pending.")
                return

            async with self.session() as session:
                completed = await Transaction.set_status_if_pending(
                    session=session, payment_id=payment_id, status=TransactionStatus.COMPLETED
                )
            if not completed:
                logger.critical(
                    f"VPN provisioning succeeded for payment {payment_id}, but completion failed. "
                    "Manual reconciliation is required."
                )
                return

        if self.config.shop.REFERRER_REWARD_ENABLED:
            await self.services.referral.add_referrers_rewards_on_payment(
                referred_tg_id=data.user_id,
                payment_amount=data.price,  # TODO: (!) add currency unified processing
                payment_id=payment_id,
            )

        await self.services.notification.notify_developer(
            text=EVENT_PAYMENT_SUCCEEDED_TAG
            + "\n\n"
            + _("payment:event:payment_succeeded").format(
                payment_id=payment_id,
                user_id=user.tg_id,
                devices=format_device_count(data.devices),
                duration=format_subscription_period(data.duration),
            ),
        )

        locale = user.language_code if user else DEFAULT_LANGUAGE
        with self.i18n.use_locale(locale):
            await redirect_to_main_menu(
                bot=self.bot,
                user=user,
                services=self.services,
                config=self.config,
                storage=self.storage,
            )

            if data.is_extend:
                logger.info(f"Subscription extended for user {user.tg_id}")
                await self.services.notification.notify_extend_success(
                    user_id=user.tg_id,
                    data=data,
                )
            elif data.is_change:
                logger.info(f"Subscription changed for user {user.tg_id}")
                await self.services.notification.notify_change_success(
                    user_id=user.tg_id,
                    data=data,
                )
            else:
                logger.info(f"Subscription created for user {user.tg_id}")
                key = await self.services.vpn.get_key(user)
                await self.services.notification.notify_purchase_success(
                    user_id=user.tg_id,
                    key=key,
                )

    async def _on_payment_canceled(self, payment_id: str) -> None:
        logger.info(f"Payment canceled {payment_id}")
        if not payment_id:
            return
        async with _payment_lock(payment_id):
            async with self.session() as session:
                transaction = await Transaction.get_by_id(session=session, payment_id=payment_id)
                if not transaction or transaction.status != TransactionStatus.PENDING:
                    return
                data = SubscriptionData.unpack(transaction.subscription)
                canceled = await Transaction.set_status_if_pending(
                    session=session, payment_id=payment_id, status=TransactionStatus.CANCELED
                )
                if not canceled:
                    return

        await self.services.notification.notify_developer(
            text=EVENT_PAYMENT_CANCELED_TAG
            + "\n\n"
            + _("payment:event:payment_canceled").format(
                payment_id=payment_id,
                user_id=data.user_id,
                devices=format_device_count(data.devices),
                duration=format_subscription_period(data.duration),
            ),
        )
