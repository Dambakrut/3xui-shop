import logging

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, PreCheckoutQuery
from aiogram.utils.i18n import gettext as _
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.models import ServicesContainer, SubscriptionData
from app.bot.payment_gateways import GatewayFactory
from app.bot.utils.formatting import format_subscription_period
from app.bot.utils.navigation import NavSubscription
from app.db.models import User
from app.bot.utils.payment_security import InvalidPayment

from .keyboard import pay_keyboard

logger = logging.getLogger(__name__)
router = Router(name=__name__)


class PaymentState(StatesGroup):
    processing = State()


@router.callback_query(SubscriptionData.filter(F.state.startswith(NavSubscription.PAY)))
async def callback_payment_method_selected(
    callback: CallbackQuery,
    user: User,
    callback_data: SubscriptionData,
    services: ServicesContainer,
    bot: Bot,
    gateway_factory: GatewayFactory,
    state: FSMContext,
) -> None:
    if (callback_data.user_id != user.tg_id or callback.from_user.id != user.tg_id
        or callback_data.devices <= 0 or callback_data.duration <= 0
        or (callback_data.is_extend and callback_data.is_change)):
        logger.warning("Payment selection user mismatch.")
        return
    if await state.get_state() == PaymentState.processing:
        logger.debug("Payment selection is already being processed.")
        return

    await state.set_state(PaymentState.processing)

    try:
        method = callback_data.state
        devices = callback_data.devices
        duration = callback_data.duration
        logger.info("Payment method selected: %s", method)
        gateway = gateway_factory.get_gateway(method)
        plan = services.plan.get_plan(devices)
        price = plan.get_price(currency=gateway.currency, duration=duration)
        callback_data.price = price

        pay_url = await gateway.create_payment(callback_data)

        if callback_data.is_extend:
            text = _("payment:message:order_extend")
        elif callback_data.is_change:
            text = _("payment:message:order_change")
        else:
            text = _("payment:message:order")

        await callback.message.edit_text(
            text=text.format(
                devices=devices,
                duration=format_subscription_period(duration),
                price=price,
                currency=gateway.currency.symbol,
            ),
            reply_markup=pay_keyboard(pay_url=pay_url, callback_data=callback_data),
        )
    except Exception:
        logger.error("Payment creation failed.")
        await services.notification.show_popup(callback=callback, text=_("payment:popup:error"))
    finally:
        await state.set_state(None)


@router.pre_checkout_query()
async def pre_checkout_handler(
    pre_checkout_query: PreCheckoutQuery, user: User, gateway_factory: GatewayFactory
) -> None:
    try:
        if pre_checkout_query.from_user.id != user.tg_id:
            raise InvalidPayment("Stars user mismatch")
        gateway = gateway_factory.get_gateway(NavSubscription.PAY_TELEGRAM_STARS)
        await gateway.validate_checkout(
            pre_checkout_query.invoice_payload, user.tg_id,
            pre_checkout_query.total_amount, pre_checkout_query.currency,
        )
        await pre_checkout_query.answer(ok=True)
    except InvalidPayment:
        logger.warning("Stars checkout validation failed.")
        await pre_checkout_query.answer(ok=False, error_message="Invalid payment order.")
    except Exception:
        logger.error("Stars checkout temporarily unavailable.")
        await pre_checkout_query.answer(ok=False, error_message="Payment temporarily unavailable.")


@router.message(F.successful_payment)
async def successful_payment(
    message: Message,
    user: User,
    session: AsyncSession,
    bot: Bot,
    gateway_factory: GatewayFactory,
) -> None:
    try:
        if message.from_user.id != user.tg_id:
            raise InvalidPayment("Stars user mismatch")
        payment = message.successful_payment
        gateway = gateway_factory.get_gateway(NavSubscription.PAY_TELEGRAM_STARS)
        await gateway.process_successful_payment(
            payment.invoice_payload, user.tg_id, payment.total_amount,
            payment.currency, payment.telegram_payment_charge_id,
        )
    except InvalidPayment:
        logger.warning("Stars successful payment validation failed.")
    except Exception:
        logger.error("Stars payment processing temporarily failed.")
        raise RuntimeError("Stars payment processing temporarily failed") from None
