import logging
import asyncio

from aiogram import Bot
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.utils.i18n import I18n
from aiogram.utils.i18n import gettext as _
from aiogram.utils.i18n import lazy_gettext as __
from aiohttp.web import Application, Request, Response
from sqlalchemy.ext.asyncio import async_sessionmaker
from yookassa import Configuration, Payment
from yookassa.domain.common.confirmation_type import ConfirmationType
from yookassa.domain.models.receipt import Receipt, ReceiptItem
from yookassa.domain.request.payment_request import PaymentRequest

from app.bot.models import ServicesContainer, SubscriptionData
from app.bot.payment_gateways import PaymentGateway
from app.bot.utils.constants import YOOKASSA_WEBHOOK, Currency
from app.bot.utils.formatting import format_device_count, format_subscription_period
from app.bot.utils.navigation import NavSubscription
from app.config import Config
from app.db.models import Transaction
from app.bot.utils.payment_security import InvalidPayment

logger = logging.getLogger(__name__)


class Yookassa(PaymentGateway):
    provider = "yookassa"
    name = ""
    currency = Currency.RUB
    callback = NavSubscription.PAY_YOOKASSA

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
        self.name = __("payment:gateway:yookassa")
        self.app = app
        self.config = config
        self.session = session
        self.storage = storage
        self.bot = bot
        self.i18n = i18n
        self.services = services

        Configuration.configure(self.config.yookassa.SHOP_ID, self.config.yookassa.TOKEN)
        self.app.router.add_post(YOOKASSA_WEBHOOK, self.webhook_handler)
        logger.info("YooKassa payment gateway initialized.")

    async def _create_invoice(self, data: SubscriptionData, transaction):
        bot_username = (await self.bot.get_me()).username
        redirect_url = f"https://t.me/{bot_username}"

        description = _("payment:invoice:description").format(
            devices=format_device_count(data.devices),
            duration=format_subscription_period(data.duration),
        )

        price = str(data.price)

        receipt = Receipt(
            customer={"email": self.config.shop.EMAIL},
            items=[
                ReceiptItem(
                    description=description,
                    quantity=1,
                    amount={"value": price, "currency": self.currency.code},
                    vat_code=1,
                )
            ],
        )

        request = PaymentRequest(
            amount={"value": price, "currency": self.currency.code},
            confirmation={"type": ConfirmationType.REDIRECT, "return_url": redirect_url},
            capture=True,
            save_payment_method=False,
            description=description,
            receipt=receipt,
            metadata={"tg_id": str(data.user_id), "subscription": data.pack()},
        )

        response = await asyncio.to_thread(Payment.create, request, transaction.purchase_flow_id)
        pay_url = response.confirmation["confirmation_url"]
        logger.info("YooKassa payment link created.")
        return pay_url, response.id, response.id

    async def handle_payment_succeeded(self, payment_id: str) -> None:
        await self._on_payment_succeeded(payment_id)

    async def handle_payment_canceled(self, payment_id: str) -> None:
        await self._on_payment_canceled(payment_id)

    async def webhook_handler(self, request: Request) -> Response:
        validated = False
        try:
            event_json = await request.json()
            payment_id = event_json.get("object", {}).get("id")
            event = event_json.get("event")
            if event not in ("payment.succeeded", "payment.canceled"):
                raise InvalidPayment("unsupported event")
            # Reject unknown IDs before an API lookup; headers are never identity evidence.
            if not isinstance(payment_id, str) or not payment_id or len(payment_id) > 64:
                raise InvalidPayment("invalid ID")
            async with self.session() as session:
                saved = await Transaction.get_by_id(session, payment_id)
            if saved is None or saved.payment_provider != self.provider:
                raise InvalidPayment("unknown order")
            payment = await asyncio.to_thread(Payment.find_one, payment_id)
            if payment.id != payment_id:
                raise InvalidPayment("payment identity mismatch")
            transaction = await self._get_verified_order(
                payment_id, payment.amount.value, payment.amount.currency, payment.id,
            )
            metadata = payment.metadata or {}
            if (metadata.get("tg_id") != str(transaction.tg_id)
                or metadata.get("subscription") != transaction.subscription):
                raise InvalidPayment("metadata mismatch")
            if event == "payment.succeeded":
                if payment.status != "succeeded" or payment.paid is not True:
                    raise InvalidPayment("payment not captured")
                validated = True
                await self.handle_payment_succeeded(payment_id)
            else:
                if payment.status != "canceled" or payment.paid is True:
                    raise InvalidPayment("payment not canceled")
                validated = True
                await self.handle_payment_canceled(payment_id)
            return Response(status=200)
        except (InvalidPayment, ValueError, TypeError, AttributeError):
            if validated:
                logger.error("YooKassa purchase processing temporarily failed.")
                return Response(status=503)
            logger.warning("YooKassa callback validation failed.")
            return Response(status=400)
        except Exception:
            logger.error("YooKassa payment verification temporarily failed.")
            return Response(status=503)
