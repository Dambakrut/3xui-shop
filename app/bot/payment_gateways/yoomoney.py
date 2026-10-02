import hashlib
import hmac
import logging
import uuid
from urllib.parse import quote

import requests
from aiogram import Bot
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.utils.i18n import I18n
from aiogram.utils.i18n import gettext as _
from aiogram.utils.i18n import lazy_gettext as __
from aiohttp.web import Application, Request, Response
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.bot.models import ServicesContainer, SubscriptionData
from app.bot.payment_gateways import PaymentGateway
from app.bot.utils.constants import YOOMONEY_WEBHOOK, Currency, TransactionStatus
from app.bot.utils.formatting import format_device_count, format_subscription_period
from app.bot.utils.navigation import NavSubscription
from app.config import Config
from app.db.models import Transaction
from app.bot.utils.payment_security import InvalidPayment, money, payment_snapshot

logger = logging.getLogger(__name__)


class Yoomoney(PaymentGateway):
    provider = "yoomoney"
    name = ""
    currency = Currency.RUB
    callback = NavSubscription.PAY_YOOMONEY

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
        self.name = __("payment:gateway:yoomoney")
        self.app = app
        self.config = config
        self.session = session
        self.storage = storage
        self.bot = bot
        self.i18n = i18n
        self.services = services

        self.app.router.add_post(YOOMONEY_WEBHOOK, self.webhook_handler)
        logger.info("YooMoney payment gateway initialized.")

    async def create_payment(self, data: SubscriptionData) -> str:
        bot_username = (await self.bot.get_me()).username
        redirect_url = f"https://t.me/{bot_username}"

        description = _("payment:invoice:description").format(
            devices=format_device_count(data.devices),
            duration=format_subscription_period(data.duration),
        )

        price = str(data.price)
        payment_id = str(uuid.uuid4())

        pay_url = self.create_quickpay_url(
            receiver=self.config.yoomoney.WALLET_ID,
            quickpay_form="shop",
            targets=description,
            paymentType="SB",
            sum=price,
            label=payment_id,
            successURL=redirect_url,
        )

        async with self.session() as session:
            transaction = await Transaction.create(
                session=session,
                tg_id=data.user_id,
                subscription=data.pack(),
                payment_id=payment_id,
                status=TransactionStatus.PENDING,
                **payment_snapshot(self.provider, self.currency.code, price),
            )
            if transaction is None:
                raise RuntimeError("Could not persist YooMoney order")

        logger.info("YooMoney payment link created.")
        return pay_url

    async def handle_payment_succeeded(self, payment_id: str) -> None:
        await self._on_payment_succeeded(payment_id)

    async def handle_payment_canceled(self, payment_id: str) -> None:
        await self._on_payment_canceled(payment_id)

    async def webhook_handler(self, request: Request) -> Response:
        validated = False
        try:
            event_data = await request.post()
            if len(event_data) != len(set(event_data.keys())):
                return Response(status=400)
            if not self.verify_notification(event_data):
                logger.warning("YooMoney signature rejected.")
                return Response(status=403)
            # Legacy SHA1 does not authenticate withdraw_amount or test flags.
            if not event_data.get("sign"):
                raise InvalidPayment("legacy signature cannot authenticate full payment")
            if (event_data.get("notification_type") not in ("p2p-incoming", "card-incoming")
                or event_data.get("codepro") != "false"
                or event_data.get("unaccepted") != "false"
                or event_data.get("test_notification", "false") != "false"
                or event_data.get("currency") != "643"):
                raise InvalidPayment("unsupported payment event")
            money(event_data.get("amount"))
            payment_id = event_data.get("label")
            await self._get_verified_order(
                payment_id, event_data.get("withdraw_amount"), self.currency.code,
            )
            async with self.session() as session:
                if not await Transaction.bind_provider_payment_id(
                    session, payment_id, event_data.get("operation_id")
                ):
                    raise InvalidPayment("operation identity mismatch")
            validated = True
            await self.handle_payment_succeeded(payment_id)
            return Response(status=200)
        except (InvalidPayment, ValueError, TypeError, AttributeError):
            if validated:
                logger.error("YooMoney purchase processing temporarily failed.")
                return Response(status=503)
            logger.warning("YooMoney callback validation failed.")
            return Response(status=400)
        except Exception:
            logger.error("YooMoney callback temporarily failed.")
            return Response(status=503)

    def create_quickpay_url(
        self,
        receiver: str,
        quickpay_form: str,
        targets: str,
        paymentType: str,
        sum: float,
        label: str = None,
        successURL: str = None,
    ) -> str:
        base_url = "https://yoomoney.ru/quickpay/confirm.xml?"
        payload = {
            "receiver": receiver,
            "quickpay_form": quickpay_form,
            "targets": targets,
            "paymentType": paymentType,
            "sum": sum,
            "label": label,
            "successURL": successURL,
        }

        query = "&".join(
            f"{key.replace('_', '-')}" + "=" + str(value)
            for key, value in payload.items()
            if value is not None
        ).replace(" ", "%20")

        response = requests.post(base_url + query)
        return response.url

    def verify_notification(self, data: dict) -> bool:
        secret = self.config.yoomoney.NOTIFICATION_SECRET
        if not secret or not all(isinstance(k, str) and isinstance(v, str) for k, v in data.items()):
            return False
        sign = data.get("sign")
        if sign is not None:
            canonical = "&".join(
                f"{key}={quote(data[key], safe='~-._')}" for key in sorted(data) if key != "sign"
            )
            expected = hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()
            return hmac.compare_digest(expected.encode(), sign.encode())
        params = [
            data.get("notification_type", ""),
            data.get("operation_id", ""),
            data.get("amount", ""),
            data.get("currency", ""),
            data.get("datetime", ""),
            data.get("sender", ""),
            data.get("codepro", ""),
            self.config.yoomoney.NOTIFICATION_SECRET,
            data.get("label", ""),
        ]

        sign_str = "&".join(params)
        computed_hash = hashlib.sha1(sign_str.encode("utf-8")).hexdigest()

        return hmac.compare_digest(computed_hash.encode(), data.get("sha1_hash", "").encode())
