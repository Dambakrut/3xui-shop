import logging

from aiogram import Bot
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import LabeledPrice
from aiogram.utils.i18n import I18n
from aiogram.utils.i18n import gettext as _
from aiogram.utils.i18n import lazy_gettext as __
from aiohttp.web import Application
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.bot.filters.is_dev import IsDev
from app.bot.models import ServicesContainer, SubscriptionData
from app.bot.payment_gateways import PaymentGateway
from app.bot.utils.constants import Currency, TransactionStatus
from app.bot.utils.formatting import format_device_count, format_subscription_period
from app.bot.utils.navigation import NavSubscription
from app.config import Config
from app.db.models import Transaction
from app.bot.utils.payment_security import InvalidPayment

logger = logging.getLogger(__name__)


class TelegramStars(PaymentGateway):
    provider = "stars"
    name = ""
    currency = Currency.XTR
    callback = NavSubscription.PAY_TELEGRAM_STARS

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
        self.name = __("payment:gateway:telegram_stars")
        self.app = app
        self.config = config
        self.session = session
        self.storage = storage
        self.bot = bot
        self.services = services
        self.i18n = i18n
        logger.info("TelegramStars payment gateway initialized.")

    async def _invoice_amount(self, data: SubscriptionData):
        amount = 1 if await IsDev()(user_id=data.user_id) else int(data.price)
        if amount <= 0:
            raise InvalidPayment("invalid Stars invoice amount")
        return amount

    async def _create_invoice(self, data: SubscriptionData, transaction):
        amount = int(transaction.expected_amount)
        order_id = transaction.payment_id
        prices = [LabeledPrice(label=self.currency.code, amount=amount)]
        devices = format_device_count(data.devices)
        duration = format_subscription_period(data.duration)
        title = _("payment:invoice:title").format(devices=devices, duration=duration)
        description = _("payment:invoice:description").format(devices=devices, duration=duration)
        pay_url = await self.bot.create_invoice_link(
            title=title,
            description=description,
            prices=prices,
            payload=order_id,
            currency=self.currency.code,
        )
        logger.info("Stars payment link created.")
        return pay_url, order_id, None

    async def validate_checkout(self, order_id, user_id, amount, currency):
        if currency != "XTR" or type(amount) is not int:
            raise InvalidPayment("invalid Stars currency or amount")
        transaction = await self._get_verified_order(order_id, amount, currency)
        if transaction.tg_id != user_id or transaction.status != TransactionStatus.PENDING:
            raise InvalidPayment("invalid Stars checkout order")
        return transaction

    async def process_successful_payment(self, order_id, user_id, amount, currency, charge_id):
        if currency != "XTR" or type(amount) is not int or not isinstance(charge_id, str) or not charge_id:
            raise InvalidPayment("invalid Stars payment")
        transaction = await self._get_verified_order(order_id, amount, currency)
        if transaction.tg_id != user_id:
            raise InvalidPayment("Stars user mismatch")
        async with self.session() as session:
            first_delivery = await Transaction.bind_provider_payment_id(
                session, order_id, charge_id, only_if_unbound=True,
            )
            if not first_delivery:
                transaction = await Transaction.get_by_id(session, order_id)
                if transaction is None or transaction.provider_payment_id != charge_id:
                    raise InvalidPayment("Stars charge identity mismatch")
        if first_delivery and await IsDev()(user_id=user_id):
            try:
                await self.bot.refund_star_payment(user_id=user_id, telegram_payment_charge_id=charge_id)
            except Exception:
                logger.error("Stars developer refund failed; manual review required.")
        await self.handle_payment_succeeded(order_id)

    async def handle_payment_succeeded(self, payment_id: str) -> None:
        await self._on_payment_succeeded(payment_id)

    async def handle_payment_canceled(self, payment_id: str) -> None:
        await self._on_payment_canceled(payment_id)
