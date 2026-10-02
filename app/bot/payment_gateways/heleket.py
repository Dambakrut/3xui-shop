import base64
import hashlib
import json
import logging
import uuid
from hmac import compare_digest

import aiohttp
from aiogram import Bot
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.utils.i18n import I18n
from aiogram.utils.i18n import gettext as _
from aiogram.utils.i18n import lazy_gettext as __
from aiohttp.web import Application, Request, Response
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.bot.models import ServicesContainer, SubscriptionData
from app.bot.payment_gateways import PaymentGateway
from app.bot.utils.constants import HELEKET_WEBHOOK, Currency, TransactionStatus
from app.bot.utils.navigation import NavSubscription
from app.config import Config
from app.db.models import Transaction
from app.bot.utils.payment_security import InvalidPayment, payment_snapshot, safe_client_ip

logger = logging.getLogger(__name__)


class Heleket(PaymentGateway):
    provider = "heleket"
    name = ""
    currency = Currency.USD
    callback = NavSubscription.PAY_HELEKET

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
        self.name = __("payment:gateway:heleket")
        self.app = app
        self.config = config
        self.session = session
        self.storage = storage
        self.bot = bot
        self.i18n = i18n
        self.services = services

        self.app.router.add_post(HELEKET_WEBHOOK, self.webhook_handler)
        logger.info("Heleket payment gateway initialized.")

    async def create_payment(self, data: SubscriptionData) -> str:
        bot_username = (await self.bot.get_me()).username
        redirect_url = f"https://t.me/{bot_username}"
        order_id = str(uuid.uuid4())
        price = str(data.price)

        payload = {
            "amount": price,
            "currency": self.currency.code,
            "order_id": order_id,
            "url_return": redirect_url,
            "url_success": redirect_url,
            "url_callback": self.config.bot.DOMAIN + HELEKET_WEBHOOK,
            "lifetime": 1800,
            "is_payment_multiple": False,
            "additional_data": str(data.user_id),
        }
        headers = {
            "merchant": self.config.heleket.MERCHANT_ID,
            "sign": self.generate_signature(json.dumps(payload)),
            "Content-Type": "application/json",
        }

        async with aiohttp.ClientSession() as session:
            url = "https://api.heleket.com/v1/payment"
            async with session.post(url, json=payload, headers=headers) as response:
                result = await response.json()
                if response.status == 200 and result.get("result", {}).get("url"):
                    pay_url = result["result"]["url"]
                else:
                    raise RuntimeError("Heleket invoice creation failed")
        invoice = result["result"]
        if invoice.get("order_id") != order_id or not invoice.get("uuid"):
            raise RuntimeError("Heleket invoice identity mismatch")

        async with self.session() as session:
            transaction = await Transaction.create(
                session=session,
                tg_id=data.user_id,
                subscription=data.pack(),
                payment_id=result["result"]["order_id"],
                status=TransactionStatus.PENDING,
                **payment_snapshot(self.provider, self.currency.code, price, invoice["uuid"]),
            )
            if transaction is None:
                raise RuntimeError("Could not persist Heleket order")

        logger.info("Heleket payment link created.")
        return pay_url

    async def handle_payment_succeeded(self, payment_id: str) -> None:
        await self._on_payment_succeeded(payment_id)

    async def handle_payment_canceled(self, payment_id: str) -> None:
        await self._on_payment_canceled(payment_id)

    async def webhook_handler(self, request: Request) -> Response:
        validated = False
        logger.debug(f"Received Heleket webhook request")
        try:
            event_json = await request.json()

            if not self.verify_webhook(request, event_json):
                return Response(status=403)
            order_id = event_json.get("order_id")
            if (event_json.get("type") != "payment"
                or event_json.get("is_final") is not True
                or not isinstance(event_json.get("uuid"), str)):
                raise InvalidPayment("invalid payment event")
            transaction = await self._get_verified_order(
                order_id, event_json.get("amount"), event_json.get("currency"), event_json["uuid"],
            )
            if event_json.get("additional_data") != str(transaction.tg_id):
                raise InvalidPayment("order metadata mismatch")
            if "merchant" in event_json and event_json["merchant"] != self.config.heleket.MERCHANT_ID:
                raise InvalidPayment("merchant mismatch")

            match event_json.get("status"):
                case "paid" | "paid_over":
                    order_id = event_json.get("order_id")
                    validated = True
                    await self.handle_payment_succeeded(order_id)
                    return Response(status=200)

                case "cancel":
                    order_id = event_json.get("order_id")
                    validated = True
                    await self.handle_payment_canceled(order_id)
                    return Response(status=200)

                case _:
                    return Response(status=400)

        except (InvalidPayment, ValueError, TypeError, AttributeError):
            if validated:
                logger.error("Heleket purchase processing temporarily failed.")
                return Response(status=503)
            logger.warning("Heleket callback validation failed.")
            return Response(status=400)
        except Exception:
            logger.error("Heleket callback temporarily failed.")
            return Response(status=503)

    def verify_webhook(self, request: Request, data: dict) -> bool:
        if not isinstance(data, dict) or not self.config.heleket.API_KEY:
            return False
        client_ip = safe_client_ip(request.remote, request.headers, getattr(self.config, "PAYMENT_TRUSTED_PROXY_NETWORKS", ()))
        if client_ip not in ["31.133.220.8"]:
            logger.warning("Heleket source rejected.")
            return False

        sign = data.get("sign")
        if not isinstance(sign, str) or len(sign) != 32:
            logger.warning("Missing signature.")
            return False

        json_data = json.dumps({k: v for k, v in data.items() if k != "sign"}, ensure_ascii=False, separators=(",", ":")).replace("/", "\\/")
        hash_value = self.generate_signature(json_data)

        if not compare_digest(hash_value.encode(), sign.encode()):
            logger.warning(f"Invalid signature.")
            return False

        return True

    def generate_signature(self, data: str) -> str:
        base64_encoded = base64.b64encode(data.encode()).decode()
        raw_string = f"{base64_encoded}{self.config.heleket.API_KEY}"
        return hashlib.md5(raw_string.encode()).hexdigest()
