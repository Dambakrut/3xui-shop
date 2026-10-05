"""Small validation helpers; never include payment payloads in exceptions."""

from decimal import Decimal, InvalidOperation
from ipaddress import ip_address, ip_network


class InvalidPayment(ValueError):
    pass


class CheckoutUnavailable(RuntimeError):
    """An existing checkout must not start another provider invoice."""


def money(value) -> Decimal:
    try:
        if isinstance(value, bool) or value is None:
            raise InvalidPayment("invalid amount")
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise InvalidPayment("invalid amount") from exc
    if not amount.is_finite() or amount <= 0:
        raise InvalidPayment("invalid amount")
    return amount


def payment_snapshot(provider, currency, amount, provider_payment_id=None):
    return dict(
        payment_provider=provider,
        expected_amount=str(money(amount)),
        expected_currency=currency,
        provider_payment_id=provider_payment_id,
    )


def validate_order(transaction, data, provider, callback, amount, currency, provider_payment_id=None):
    if not transaction or transaction.payment_provider != provider:
        raise InvalidPayment("unknown order or provider")
    if data.user_id != transaction.tg_id or data.state != callback:
        raise InvalidPayment("order identity mismatch")
    if data.devices <= 0 or data.duration <= 0 or (data.is_extend and data.is_change):
        raise InvalidPayment("invalid subscription")
    if currency != transaction.expected_currency:
        raise InvalidPayment("currency mismatch")
    if money(amount) != money(transaction.expected_amount):
        raise InvalidPayment("amount mismatch")
    if provider_payment_id is not None and transaction.provider_payment_id != provider_payment_id:
        raise InvalidPayment("provider payment mismatch")
    return data


def safe_client_ip(remote, headers, trusted_networks=()):
    """Walk XFF right to left, only while the current hop is explicitly trusted."""
    try:
        current = ip_address(remote)
        networks = [ip_network(value) for value in trusted_networks]
        if not any(current in network for network in networks):
            return str(current)
        forwarded = headers.get("X-Forwarded-For", "")
        if not forwarded:
            return str(current)
        hops = forwarded.split(",")
        if len(hops) > 20:
            return None
        for hop in reversed(hops):
            if not any(current in network for network in networks):
                break
            current = ip_address(hop.strip())
        return str(current)
    except (ValueError, TypeError):
        return None
