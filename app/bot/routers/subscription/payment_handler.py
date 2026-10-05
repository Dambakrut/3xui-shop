import logging
import re

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, PreCheckoutQuery
from aiogram.utils.i18n import gettext as _
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.models import CheckoutData, ServicesContainer, SubscriptionData
from app.bot.payment_gateways import GatewayFactory
from app.bot.utils.formatting import format_subscription_period
from app.bot.utils.navigation import NavSubscription
from app.db.models import Transaction, User
from app.bot.utils.payment_security import CheckoutUnavailable, InvalidPayment

from .keyboard import pay_keyboard

logger = logging.getLogger(__name__)
router = Router(name=__name__)


class PaymentState(StatesGroup):
    processing = State()


@router.callback_query(CheckoutData.filter())
async def callback_payment_method_selected(
    callback: CallbackQuery,
    user: User,
    callback_data: CheckoutData,
    services: ServicesContainer,
    bot: Bot,
    gateway_factory: GatewayFactory,
    state: FSMContext,
) -> None:
    if (callback.from_user.id != user.tg_id
            or not re.fullmatch(r"[0-9a-f]{32}", callback_data.flow_id)):
        logger.warning("Invalid checkout reference")
        return
    try:
        gateway = next((g for g in gateway_factory.get_gateways()
                        if g.provider == callback_data.provider), None)
        if gateway is None:
            raise InvalidPayment("unknown checkout provider")
        async with gateway.session() as session:
            saved = await Transaction.get_by_purchase_flow(session, callback_data.flow_id)
        if saved is not None:
            if saved.tg_id != user.tg_id or saved.payment_provider != gateway.provider:
                raise InvalidPayment("checkout owner/provider mismatch")
            data = SubscriptionData.unpack(saved.subscription)
        else:
            context = await state.get_data()
            if context.get("checkout_flow_id") != callback_data.flow_id:
                raise InvalidPayment("expired checkout context")
            data = SubscriptionData.unpack(context["checkout_subscription"])
            if data.user_id != user.tg_id:
                raise InvalidPayment("checkout user mismatch")
            plan = services.plan.get_plan(data.devices)
            if plan is None or data.duration not in services.plan.get_durations():
                raise InvalidPayment("invalid checkout plan")
            data.state = gateway.callback
            data.price = plan.get_price(currency=gateway.currency, duration=data.duration)

        # DB unique reservation owns provider I/O; FSM is not a lock or proof.
        await state.set_state(PaymentState.processing)
        pay_url = await gateway.create_payment(data, purchase_flow_id=callback_data.flow_id)
        # Always display the immutable original snapshot, even after repricing.
        async with gateway.session() as session:
            saved = await Transaction.get_by_purchase_flow(session, callback_data.flow_id)
        data = SubscriptionData.unpack(saved.subscription)
        if data.is_extend:
            text = _("payment:message:order_extend")
        elif data.is_change:
            text = _("payment:message:order_change")
        else:
            text = _("payment:message:order")
        try:
            await callback.message.edit_text(
                text=text.format(devices=data.devices,
                    duration=format_subscription_period(data.duration),
                    price=data.price, currency=gateway.currency.symbol),
                reply_markup=pay_keyboard(pay_url=pay_url, callback_data=data),
            )
        except TelegramBadRequest as error:
            if "message is not modified" not in error.message.lower():
                raise
    except CheckoutUnavailable:
        await services.notification.show_popup(callback=callback, text=_("payment:popup:checkout_handled"))
    except InvalidPayment:
        logger.warning("Checkout reference rejected")
        await services.notification.show_popup(callback=callback, text=_("payment:popup:checkout_expired"))
    except Exception:
        logger.error("Payment creation failed")
        await services.notification.show_popup(callback=callback, text=_("payment:popup:error"))
    finally:
        await state.set_state(None)


@router.callback_query(SubscriptionData.filter(F.state.startswith(NavSubscription.PAY)))
async def callback_legacy_payment_selection(callback: CallbackQuery, services: ServicesContainer) -> None:
    # Old buttons have no durable checkout identity; never create from them.
    await services.notification.show_popup(callback=callback, text=_("payment:popup:checkout_expired"))


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
