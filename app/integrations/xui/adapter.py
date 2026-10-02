"""Async HTTP adapter for MHSanaei/3x-ui v3.8.5."""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import quote, urlsplit

import aiohttp

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
    XUIMutationResponseMetadata,
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
        """Session CSRF token used for authenticated POST requests."""
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
        mutation: bool = False,
        response_metadata: XUIMutationResponseMetadata | None = None,
    ) -> Any:
        session = self._get_session()
        headers = {"Accept": "application/json"}
        if self.auth_mode is XUIAuthMode.TOKEN:
            headers["Authorization"] = f"Bearer {self._token}"
        elif login or mutation:
            if not self._csrf_token:
                raise XUIAuthenticationError("CSRF token is unavailable")
            headers["X-CSRF-Token"] = self._csrf_token
        try:
            async with session.request(
                method, self._url(endpoint), json=json_body, headers=headers,
                allow_redirects=False,
            ) as response:
                status = response.status
                if response_metadata is not None:
                    response_metadata.response_received = True
                    response_metadata.http_status = status
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
                    if response_metadata is not None:
                        response_metadata.inspect_envelope(payload)
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
        if response_metadata is not None:
            response_metadata.valid_mutation_response = True
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

    async def get_subscription_base_url(self) -> str:
        """Read the panel's explicit subURI; never infer a reverse proxy URL."""
        if not self._authenticated:
            await self.authenticate()
        # v3.8.5 names this read-only settings route POST /setting/all.
        value = await self._request("POST", "panel/api/setting/all", json_body={}, mutation=True)
        if not isinstance(value, dict):
            raise XUIProtocolError("Invalid panel settings response")
        uri = value.get("subURI")
        if not isinstance(uri, str) or not uri:
            raise XUIProtocolError("Panel subURI is not explicitly configured")
        return self.validate_subscription_base_url(uri)

    @staticmethod
    def validate_subscription_base_url(uri: str) -> str:
        try:
            parts = urlsplit(uri)
            parts.port
        except ValueError as exc:
            raise XUIProtocolError("Invalid subscription base URL") from exc
        if (any(c.isspace() or ord(c) < 32 for c in uri)
                or "\\" in uri or parts.scheme != "https" or not parts.hostname or parts.username
                or parts.password or parts.query or parts.fragment or not parts.path
                or not parts.path.endswith("/")):
            raise XUIProtocolError("Subscription base URL must be an HTTPS URL ending in /")
        return uri

    @staticmethod
    def _matches(client: XUIClient, desired: XUIClientWrite) -> bool:
        return (
            client.email == desired.email and client.uuid == desired.uuid
            and set(client.inbound_ids) == set(desired.inbound_ids)
            and client.expiry_time_ms == desired.expiry_time_ms
            and client.total_bytes == desired.total_bytes
            and client.limit_ip == desired.limit_ip
            and client.limit_hwid == desired.limit_hwid
            and client.tg_id == desired.tg_id
            and client.sub_id == desired.sub_id
            and client.enable is desired.enable
            and client.flow == desired.flow and client.comment == desired.comment
            and client.reset == desired.reset
            and all(XUIClientWrite.from_client(
                client, expiry_time_ms=desired.expiry_time_ms,
                limit_ip=client.limit_ip, enable=client.enable,
            ).preserved.get(key) == value for key, value in desired.preserved.items())
        )

    async def _read_after_write(self, desired: XUIClientWrite) -> XUIClient:
        try:
            client = await self.get_client(desired.email)
        except XUIError as exc:
            raise XUIReconciliationError("Client write outcome needs review") from exc
        if not self._matches(client, desired):
            raise XUIReconciliationError("Client write was not confirmed")
        return client

    async def _mutate(self, endpoint: str, body: dict[str, Any],
                      desired: XUIClientWrite) -> XUIWriteResult:
        if not self._authenticated:
            await self.authenticate()
        metadata = XUIMutationResponseMetadata()
        try:
            obj = await self._request("POST", endpoint, json_body=body, mutation=True,
                                      response_metadata=metadata)
        except XUIError as exc:
            # A failed response, malformed envelope, timeout or connection reset
            # can all follow a partial commit in v3.8.5. Never replay POST.
            try:
                client = await self._read_after_write(desired)
            except XUIReconciliationError as review:
                raise review from exc
            return XUIWriteResult(
                client=client,
                node_pending=None, reconciled=True,
                response_metadata=metadata,
            )
        if obj is None:
            # v3.8.5 pendingNodeObj(false) returns nil. Only a received,
            # validated successful response establishes this, never a GET.
            node_pending = False if metadata.valid_mutation_response else None
        elif isinstance(obj, dict) and type(obj.get("nodePending")) is bool:
            node_pending = obj["nodePending"]
        else:
            # The envelope was successful but its outcome object is unknown.
            # Neither create nor update emits another successful object in
            # v3.8.5. Unknown shapes cannot establish node activation.
            node_pending = None
        client = await self._read_after_write(desired)
        return XUIWriteResult(client=client, node_pending=node_pending, reconciled=True,
                              response_metadata=metadata)

    async def add_client(self, desired: XUIClientWrite) -> XUIWriteResult:
        """Create one explicit membership, with pre-read and post-read guards."""
        if len(desired.inbound_ids) != 1:
            raise ValueError("Shop create requires exactly one configured inbound")
        try:
            existing = await self.get_client(desired.email)
        except XUINotFoundError:
            existing = None
        if existing is not None:
            if not self._matches(existing, desired):
                raise XUIAmbiguousWriteError("Existing client differs from requested create")
            return XUIWriteResult(client=existing, node_pending=None, reconciled=True)
        return await self._mutate(
            "panel/api/clients/add",
            {"client": desired.client_payload(), "inboundIds": list(desired.inbound_ids)},
            desired,
        )

    async def update_client(self, current: XUIClient,
                            desired: XUIClientWrite) -> XUIWriteResult:
        """Full replacement body, preserving known fields and all memberships."""
        if (current.email != desired.email or current.uuid != desired.uuid
                or set(current.inbound_ids) != set(desired.inbound_ids)):
            raise XUIAmbiguousWriteError("Client identity or membership changed")
        latest = await self.get_client(current.email)
        if self._matches(latest, desired):
            return XUIWriteResult(client=latest, node_pending=None, reconciled=True)
        if (latest.uuid != current.uuid or set(latest.inbound_ids) != set(current.inbound_ids)
                or latest.raw != current.raw):
            raise XUIAmbiguousWriteError("Client changed since preservation snapshot")
        return await self._mutate(
            f"panel/api/clients/update/{self._segment(desired.email, 'email')}",
            desired.client_payload(), desired,
        )
