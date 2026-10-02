"""Errors exposed by the read-only 3x-ui adapter.

Messages deliberately exclude URLs, provider messages and response bodies.
"""


class XUIError(Exception):
    """Base error for a panel operation."""


class XUITransportError(XUIError):
    """Connection, TLS, timeout or other HTTP transport failure."""


class XUIAuthenticationError(XUIError):
    """Session login failed or credentials/token were rejected."""


class XUIAuthorizationError(XUIError):
    """Authenticated caller lacks the required API scope."""


class XUINotFoundError(XUIError):
    """A requested resource is absent."""


class XUIProtocolError(XUIError):
    """The response does not match the checked 3x-ui 3.8.5 contract."""


class XUIAPIError(XUIError):
    """The panel returned a valid negative API response."""
