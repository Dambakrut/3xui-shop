"""Read-only HTTP adapter for MHSanaei/3x-ui v3.8.5.

This module is intentionally not wired into the shop's provisioning path.
"""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import quote, urlsplit

import aiohttp

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
)


class XUIAdapter:
    """Owns one cookie jar and connection pool for one panel/server.

    Token mode requires an admin-scope token for the complete read surface.
    A monitor token works only with get_server_status; node-sync does not
    authorize canonical client/traffic/link reads on v3.8.5.
    """

    def __init__(
        self,
        base_url: str,
        *,
        auth_mode: XUIAuthMode = XUIAuthMode.SESSION,
        username: str | None = None,
        password: str | None = None,
        token: str | None = None,
        two_factor_code: str | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        parts = urlsplit(base_url)
        if (parts.scheme not in ("http", "https") or not parts.hostname
                or parts.username or parts.password or parts.query or parts.fragment):
            raise ValueError("base_url must be an HTTP(S) panel URL without credentials or query")
        if type(timeout_seconds) not in (int, float) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        try:
            self.auth_mode = XUIAuthMode(auth_mode)
        except ValueError as exc:
            raise ValueError("Unsupported XUI auth mode") from exc
        if self.auth_mode is XUIAuthMode.SESSION:
            if not username or not password or token is not None:
                raise ValueError("Session mode requires username/password and no API token")
        elif not token or username is not None or password is not None or two_factor_code is not None:
            raise ValueError("Token mode requires only a Bearer API token")

        self._base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._token = token
        self._two_factor_code = two_factor_code
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._session: aiohttp.ClientSession | None = None
        self._authenticated = False
        self._csrf_token: str | None = None
        self._auth_lock = asyncio.Lock()
        self._closed = False

    @property
    def csrf_token(self) -> str | None:
        """Session CSRF token retained for a later write-capable adapter patch."""
        return self._csrf_token

    async def __aenter__(self) -> XUIAdapter:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    def _get_session(self) -> aiohttp.ClientSession:
        if self._closed:
            raise XUIError("Adapter is closed")
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=self._timeout,
                cookie_jar=aiohttp.CookieJar(),
                trust_env=False,
            )
        return self._session

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._authenticated = False
            self._csrf_token = None
            if self._session is not None:
                await self._session.close()

    def _url(self, endpoint: str) -> str:
        return f"{self._base_url}/{endpoint.lstrip('/')}"

    @staticmethod
    def _segment(value: str, name: str) -> str:
        if not isinstance(value, str) or not value or any(ord(char) < 32 for char in value):
            raise ValueError(f"{name} must be a nonempty path segment")
        return quote(value, safe="")

    async def _request(
        self, method: str, endpoint: str, *, json_body: dict[str, Any] | None = None,
        login: bool = False, csrf_bootstrap: bool = False, not_found_on_gorm: bool = False,
    ) -> Any:
        session = self._get_session()
        headers = {"Accept": "application/json"}
        if self.auth_mode is XUIAuthMode.TOKEN:
            headers["Authorization"] = f"Bearer {self._token}"
        elif login:
            if not self._csrf_token:
                raise XUIAuthenticationError("CSRF token is unavailable")
            headers["X-CSRF-Token"] = self._csrf_token
        try:
            async with session.request(
                method, self._url(endpoint), json=json_body, headers=headers,
                allow_redirects=False,
            ) as response:
                status = response.status
                if status == 401 or (status == 404 and self.auth_mode is XUIAuthMode.SESSION
                                     and not csrf_bootstrap and not login):
                    raise XUIAuthenticationError("Panel authentication failed")
                if status == 403:
                    if login or csrf_bootstrap:
                        raise XUIAuthenticationError("Panel authentication failed")
                    raise XUIAuthorizationError("Panel API access denied")
                if status == 404:
                    raise XUINotFoundError("Panel endpoint or resource not found")
                if not 200 <= status < 300:
                    raise XUITransportError(f"Panel HTTP error {status}")
                try:
                    payload = await response.json(content_type=None)
                except (ValueError, aiohttp.ContentTypeError) as exc:
                    raise XUIProtocolError("Panel returned invalid JSON") from exc
        except XUIError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise XUITransportError("Panel request failed") from exc

        if not isinstance(payload, dict) or type(payload.get("success")) is not bool:
            raise XUIProtocolError("Invalid panel response envelope")
        if payload["success"] is False:
            if login or csrf_bootstrap:
                raise XUIAuthenticationError("Panel authentication failed")
            message = payload.get("msg")
            if (not_found_on_gorm and isinstance(message, str)
                    and "record not found" in message.lower()):
                raise XUINotFoundError("Panel client not found")
            raise XUIAPIError("Panel API rejected request")
        if not login and "obj" not in payload:
            raise XUIProtocolError("Panel response is missing obj")
        return payload.get("obj")

    async def authenticate(self) -> None:
        """Bootstrap CSRF and login, or prepare Bearer mode without login."""
        if self._closed:
            raise XUIError("Adapter is closed")
        async with self._auth_lock:
            if self._authenticated:
                return
            if self.auth_mode is XUIAuthMode.TOKEN:
                self._authenticated = True
                return
            self._csrf_token = None
            token = await self._request("GET", "csrf-token", csrf_bootstrap=True)
            if not isinstance(token, str) or not token:
                raise XUIProtocolError("Invalid CSRF bootstrap response")
            self._csrf_token = token
            body = {"username": self._username, "password": self._password}
            if self._two_factor_code is not None:
                body["twoFactorCode"] = self._two_factor_code
            try:
                await self._request("POST", "login", json_body=body, login=True)
            except Exception:
                self._csrf_token = None
                self._authenticated = False
                raise
            if not self._get_session().cookie_jar.filter_cookies(self._base_url).get("3x-ui"):
                self._csrf_token = None
                raise XUIAuthenticationError("Panel did not establish a session")
            self._authenticated = True

    async def _get(self, endpoint: str, *, not_found_on_gorm: bool = False) -> Any:
        if not self._authenticated:
            await self.authenticate()
        try:
            return await self._request("GET", endpoint, not_found_on_gorm=not_found_on_gorm)
        except XUIAuthenticationError:
            # A later call may bootstrap a fresh session. Never replay the GET
            # within the same call after an ambiguous authentication failure.
            self._authenticated = False
            self._csrf_token = None
            raise

    async def get_server_status(self) -> XUIServerStatus:
        return XUIServerStatus.from_api(await self._get("panel/api/server/status"))

    async def list_inbounds(self) -> tuple[XUIInboundSummary, ...]:
        value = await self._get("panel/api/inbounds/list")
        if not isinstance(value, list):
            raise XUIProtocolError("Panel inbound list is not an array")
        inbounds = tuple(XUIInboundSummary.from_api(item) for item in value)
        if len({item.id for item in inbounds}) != len(inbounds):
            raise XUIProtocolError("Duplicate inbound ID")
        return inbounds

    async def get_inbound(self, inbound_id: int) -> XUIInboundSummary:
        if type(inbound_id) is not int or inbound_id <= 0:
            raise ValueError("inbound_id must be a positive integer")
        for inbound in await self.list_inbounds():
            if inbound.id == inbound_id:
                return inbound
        raise XUINotFoundError("Configured inbound not found")

    async def get_client(self, email: str) -> XUIClient:
        encoded = self._segment(email, "email")
        value = await self._get(f"panel/api/clients/get/{encoded}", not_found_on_gorm=True)
        if value is None:
            raise XUINotFoundError("Panel client not found")
        client = XUIClient.from_api(value)
        if client.email != email:
            raise XUIProtocolError("Panel returned a different client")
        return client

    async def get_client_traffic(self, email: str) -> XUIClientTraffic | None:
        encoded = self._segment(email, "email")
        value = await self._get(f"panel/api/clients/traffic/{encoded}")
        if value is None:  # v3.8.5 returns null when no traffic row exists.
            return None
        traffic = XUIClientTraffic.from_api(value)
        if traffic.email != email:
            raise XUIProtocolError("Panel returned traffic for a different client")
        return traffic

    async def get_subscription_links(self, sub_id: str) -> tuple[str, ...]:
        """Return protocol share links, not an HTTP subscription URL.

        v3.8.5 /clients/subLinks yields vless:// etc. The 6A audit called
        these subscription links; the source does not return the subscription
        endpoint URL used by VPNService.get_key.
        """
        encoded = self._segment(sub_id, "sub_id")
        value = await self._get(f"panel/api/clients/subLinks/{encoded}")
        if not isinstance(value, list) or any(not isinstance(link, str) or not link for link in value):
            raise XUIProtocolError("Invalid subscription links response")
        return tuple(value)
