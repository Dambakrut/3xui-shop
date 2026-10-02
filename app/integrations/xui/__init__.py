"""Read-only 3x-ui v3.8.5 adapter foundation."""

from .adapter import XUIAdapter
from .exceptions import (
    XUIAPIError,
    XUIAuthenticationError,
    XUIAuthorizationError,
    XUIError,
    XUINotFoundError,
    XUIProtocolError,
    XUITransportError,
)
from .models import (
    XUIAuthMode,
    XUIClient,
    XUIClientTraffic,
    XUIInboundSummary,
    XUIServerStatus,
    is_member_of,
    validate_client_membership,
)

__all__ = [
    "XUIAdapter", "XUIAuthMode", "XUIClient", "XUIClientTraffic", "XUIInboundSummary",
    "XUIServerStatus", "is_member_of", "validate_client_membership", "XUIError",
    "XUITransportError", "XUIAuthenticationError", "XUIAuthorizationError",
    "XUINotFoundError", "XUIProtocolError", "XUIAPIError",
]
