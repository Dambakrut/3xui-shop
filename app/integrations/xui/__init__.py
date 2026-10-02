"""3x-ui v3.8.5 adapter."""

from .adapter import XUIAdapter
from .exceptions import (
    XUIAPIError,
    XUIAmbiguousWriteError,
    XUIAuthenticationError,
    XUIAuthorizationError,
    XUIError,
    XUINotFoundError,
    XUIProtocolError,
    XUIReconciliationError,
    XUITransportError,
)
from .models import (
    XUIAuthMode,
    XUIClient,
    XUIClientTraffic,
    XUIClientWrite,
    XUIInboundSummary,
    XUIServerStatus,
    XUIWriteResult,
    is_member_of,
    validate_client_membership,
)

__all__ = [
    "XUIAdapter", "XUIAuthMode", "XUIClient", "XUIClientTraffic", "XUIInboundSummary",
    "XUIClientWrite", "XUIWriteResult", "XUIAmbiguousWriteError", "XUIReconciliationError",
    "XUIServerStatus", "is_member_of", "validate_client_membership", "XUIError",
    "XUITransportError", "XUIAuthenticationError", "XUIAuthorizationError",
    "XUINotFoundError", "XUIProtocolError", "XUIAPIError",
]
