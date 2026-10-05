from aiogram.filters.callback_data import CallbackData


class CheckoutData(CallbackData, prefix="checkout"):
    """Compact reference to a server-side checkout, not a client supplied cart."""
    flow_id: str
    provider: str
