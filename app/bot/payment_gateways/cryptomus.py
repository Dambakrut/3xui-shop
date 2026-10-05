import base64
import hashlib
import json
import logging
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
from app.bot.utils.constants import CRYPTOMUS_WEBHOOK, Currency
from app.bot.utils.navigation import NavSubscription
from app.config import Config
from app.db.models import Transaction
from app.bot.utils.payment_security import InvalidPayment, safe_client_ip

logger = logging.getLogger(__name__)


class Cryptomus(PaymentGateway):
    provider = "cryptomus"
    name = ""
    currency = Currency.USD
    callback = NavSubscription.PAY_CRYPTOMUS

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
        self.name = __("payment:gateway:cryptomus")
        self.app = app
        self.config = config
        self.session = session
        self.storage = storage
        self.bot = bot
        self.i18n = i18n
        self.services = services

        self.app.router.add_post(CRYPTOMUS_WEBHOOK, self.webhook_handler)
        logger.info("Cryptomus payment gateway initialized.")

    async def _create_invoice(self, data: SubscriptionData, transaction):
        bot_username = (await self.bot.get_me()).username
        redirect_url = f"https://t.me/{bot_username}"
        order_id = transaction.payment_id
        price = str(data.price)

        payload = {
            "amount": price,
            "currency": self.currency.code,
            "order_id": order_id,
            "url_return": redirect_url,
            "url_success": redirect_url,
            "url_callback": self.config.bot.DOMAIN + CRYPTOMUS_WEBHOOK,
            "lifetime": 1800,
            "is_payment_multiple": False,
            "additional_data": str(data.user_id),
        }
        headers = {
            "merchant": self.config.cryptomus.MERCHANT_ID,
            "sign": self.generate_signature(json.dumps(payload)),
            "Content-Type": "application/json",
        }

        async with aiohttp.ClientSession() as session:
            url = "https://api.cryptomus.com/v1/payment"
            async with session.post(url, json=payload, headers=headers) as response:
                result = await response.json()
                if response.status == 200 and result.get("result", {}).get("url"):
                    pay_url = result["result"]["url"]
                else:
                    raise RuntimeError("Cryptomus invoice creation failed")
        invoice = result["result"]
        if invoice.get("order_id") != order_id or not invoice.get("uuid"):
            raise RuntimeError("Cryptomus invoice identity mismatch")
        logger.info("Cryptomus payment link created.")
        return pay_url, order_id, invoice["uuid"]

    async def handle_payment_succeeded(self, payment_id: str) -> None:
        await self._on_payment_succeeded(payment_id)

    async def handle_payment_canceled(self, payment_id: str) -> None:
        await self._on_payment_canceled(payment_id)

    async def webhook_handler(self, request: Request) -> Response:
        validated = False
        logger.debug(f"Received Cryptomus webhook request")
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
            if "merchant" in event_json and event_json["merchant"] != self.config.cryptomus.MERCHANT_ID:
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
                logger.error("Cryptomus purchase processing temporarily failed.")
                return Response(status=503)
            logger.warning("Cryptomus callback validation failed.")
            return Response(status=400)
        except Exception:
            logger.error("Cryptomus callback temporarily failed.")
            return Response(status=503)

    def verify_webhook(self, request: Request, data: dict) -> bool:
        if not isinstance(data, dict) or not self.config.cryptomus.API_KEY:
            return False
        client_ip = safe_client_ip(request.remote, request.headers, getattr(self.config, "PAYMENT_TRUSTED_PROXY_NETWORKS", ()))
        if client_ip not in ["91.227.144.54"]:
            logger.warning("Cryptomus source rejected.")
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
        raw_string = f"{base64_encoded}{self.config.cryptomus.API_KEY}"
        return hashlib.md5(raw_string.encode()).hexdigest()
