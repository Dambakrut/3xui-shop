from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .server_pool import ServerPoolService

import logging
from urllib.parse import quote

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.bot.models import ClientData
from app.bot.utils.time import (
    add_days_to_timestamp,
    days_to_timestamp,
    get_current_timestamp,
)
from app.config import Config
from app.db.models import Promocode, User
from app.integrations.xui import (
    XUIAdapter, XUIAmbiguousWriteError, XUIClient, XUIClientWrite, XUIError,
    XUINotFoundError, validate_client_membership,
)

logger = logging.getLogger(__name__)


class VPNReadError(RuntimeError):
    """Generic service error for an unavailable or untrusted panel read."""


class VPNService:
    def __init__(
        self,
        config: Config,
        session: async_sessionmaker,
        server_pool_service: ServerPoolService,
    ) -> None:
        self.config = config
        self.session = session
        self.server_pool_service = server_pool_service
        logger.info("VPN Service initialized.")

    async def _read_owned_client(self, user: User, connection) -> XUIClient | None:
        """Read canonical identity and membership; never trust traffic row ID."""
        try:
            client = await connection.adapter.get_client(str(user.tg_id))
        except XUINotFoundError:
            return None
        except XUIError as exception:
            raise VPNReadError("VPN client lookup unavailable") from exception
        if client.uuid != user.vpn_id or client.email != str(user.tg_id):
            raise VPNReadError("VPN client identity mismatch")
        try:
            validate_client_membership(client, self.config.xui.INBOUND_ID)
        except XUIError as exception:
            raise VPNReadError("VPN client is outside the configured inbound") from exception
        return client

    async def is_client_exists(self, user: User) -> XUIClient | None:
        connection = await self.server_pool_service.get_connection(user)

        if not connection:
            return None

        client = await self._read_owned_client(user, connection)

        if client:
            logger.debug(f"Client {user.tg_id} exists on server {connection.server.name}.")
        else:
            logger.critical(f"Client {user.tg_id} not found on server {connection.server.name}.")

        return client

    async def get_limit_ip(self, user: User, client: XUIClient) -> int | None:
        """Read configured client's IP limit from canonical data, never inbound order."""
        try:
            if client.uuid != user.vpn_id or client.email != str(user.tg_id):
                logger.error("VPN client identity mismatch for %s", user.tg_id)
                return None
            validate_client_membership(client, self.config.xui.INBOUND_ID)
        except XUIError:
            logger.error("Client %s is absent from configured inbound", user.tg_id)
            return None
        return client.limit_ip

    async def get_client_data(self, user: User) -> ClientData | None:
        logger.debug(f"Starting to retrieve client data for {user.tg_id}.")

        connection = await self.server_pool_service.get_connection(user)

        if not connection:
            return None

        try:
            client = await self._read_owned_client(user, connection)

            if not client:
                logger.critical(
                    f"Client {user.tg_id} not found on server {connection.server.name}."
                )
                return None

            traffic = await connection.adapter.get_client_traffic(str(user.tg_id))
            if traffic is None:
                logger.warning("No traffic row for client %s", user.tg_id)
                return None

            limit_ip = await self.get_limit_ip(user=user, client=client)
            if limit_ip is None:
                return None
            max_devices = -1 if limit_ip == 0 else limit_ip
            traffic_total = traffic.total
            expiry_time = -1 if traffic.expiry_time_ms == 0 else traffic.expiry_time_ms

            if traffic_total <= 0:
                traffic_remaining = -1
                traffic_total = -1
            else:
                traffic_remaining = traffic.total - (traffic.up + traffic.down)

            traffic_used = traffic.up + traffic.down
            client_data = ClientData(
                max_devices=max_devices,
                traffic_total=traffic_total,
                traffic_remaining=traffic_remaining,
                traffic_used=traffic_used,
                traffic_up=traffic.up,
                traffic_down=traffic.down,
                expiry_time=expiry_time,
            )
            logger.debug(f"Successfully retrieved client data for {user.tg_id}: {client_data}.")
            return client_data
        except Exception as exception:
            logger.error(f"Error retrieving client data for {user.tg_id}: {exception}")
            return None

    async def get_key(self, user: User) -> str | None:
        async with self.session() as session:
            user = await User.get(session=session, tg_id=user.tg_id)

        if not user.server_id:
            logger.debug(f"Server ID for user {user.tg_id} not found.")
            return None

        connection = await self.server_pool_service.get_connection(user)
        if connection is None:
            return None
        client = await self._read_owned_client(user, connection)
        if client is None:
            return None
        if not client.sub_id or any(c.isspace() or ord(c) < 32 or c in "/\\" for c in client.sub_id):
            raise VPNReadError("Invalid canonical subscription identifier")
        subscription = self.config.xui.SUBSCRIPTION_BASE_URL
        if not subscription:
            subscription = await connection.adapter.get_subscription_base_url()
        subscription = XUIAdapter.validate_subscription_base_url(subscription)
        key = f"{subscription.rstrip('/')}/{quote(client.sub_id, safe='')}"
        logger.debug("Subscription URL retrieved for user %s", user.tg_id)
        return key

    @staticmethod
    def _validate_provisioning_capability(inbound, requested_flow: str | None) -> str:
        """Resolve a verified transport policy; never rewrite an existing flow.

        None selects the create default. Renewal passes the canonical flow and
        validates every attachment. See docs/3xui-adapter.md for source rules.
        """
        policies = {
            ("vless", "tls", "tcp"): ("xtls-rprx-vision", ("", "xtls-rprx-vision")),
            ("vless", "reality", "tcp"): ("xtls-rprx-vision", ("", "xtls-rprx-vision")),
            ("vless", "reality", "xhttp"): ("", ("",)),
        }
        stream = inbound.stream_settings or {}
        policy = policies.get((inbound.protocol, stream.get("security"), stream.get("network")))
        if not inbound.enable or policy is None:
            raise VPNReadError("Inbound has an unsupported shop provisioning capability")
        default_flow, allowed_flows = policy
        flow = default_flow if requested_flow is None else requested_flow
        if flow not in allowed_flows:
            raise VPNReadError("Client flow is incompatible with inbound capability")
        disable_flow = inbound.raw.get("disableFlow", False)
        if type(disable_flow) is not bool:
            raise VPNReadError("Inbound disableFlow is malformed")
        if stream.get("network") == "tcp" and disable_flow:
            # Preserve the existing TCP shop contract; XHTTP's empty flow does
            # not depend on this panel switch (clientWithInboundFlow).
            raise VPNReadError("TCP shop provisioning requires flow enabled")
        if flow == "":
            protocol_settings = inbound.raw.get("settings")
            # Xray's VLessInboundConfig inherits settings.flow when the client
            # flow is empty. Prove that empty really remains empty.
            if not isinstance(protocol_settings, dict) or protocol_settings.get("flow", "") != "":
                raise VPNReadError("Empty client flow requires verified empty inbound flow")
        if stream.get("network") == "xhttp":
            xhttp = stream.get("xhttpSettings")
            if not isinstance(xhttp, dict) or xhttp.get("mode", "") not in (
                "", "auto", "stream-one", "stream-up", "packet-up",
            ):
                raise VPNReadError("XHTTP transport settings are unsupported or unavailable")
        return flow

    async def create_client(
        self,
        user: User,
        devices: int,
        duration: int,
        enable: bool = True,
        flow: str | None = None,
        total_gb: int = 0,
    ) -> bool:
        logger.info(f"Creating new client {user.tg_id} | {devices} devices {duration} days.")

        connection = (
            await self.server_pool_service.get_connection(user)
            if user.server_id
            else await self.server_pool_service.get_provisioning_connection()
        )

        if not connection:
            return False

        inbound = await self.server_pool_service.validate_configured_inbound(connection.adapter)
        if inbound is None:
            return False
        if duration <= 0 or devices < 0 or total_gb < 0:
            return False
        try:
            write = XUIClientWrite(
                email=str(user.tg_id), uuid=user.vpn_id,
                inbound_ids=(self.config.xui.INBOUND_ID,),
                expiry_time_ms=days_to_timestamp(duration), total_bytes=total_gb,
                limit_ip=devices, limit_hwid=0, tg_id=user.tg_id,
                sub_id=user.vpn_id, enable=enable,
                flow=self._validate_provisioning_capability(inbound, flow),
            )
            result = await connection.adapter.add_client(write)
            if not user.server_id and not await self.server_pool_service.assign_server_to_user(
                user, connection.server
            ):
                raise XUIAmbiguousWriteError("Panel client persisted but server assignment failed")
            if result.node_pending is not False:
                raise XUIAmbiguousWriteError("Client persisted; node activation needs review")
            logger.info(f"Successfully created client for {user.tg_id}")
            return True
        except XUIAmbiguousWriteError:
            raise
        except Exception as exception:
            logger.error("Error creating client for %s (%s)", user.tg_id, type(exception).__name__)
            return False

    async def update_client(
        self,
        user: User,
        devices: int,
        duration: int,
        replace_devices: bool = False,
        replace_duration: bool = False,
        enable: bool = True,
        flow: str | None = None,
        total_gb: int = 0,
    ) -> bool:
        logger.info(f"Updating client {user.tg_id} | {devices} devices {duration} days.")
        connection = await self.server_pool_service.get_connection(user)

        if not connection:
            return False

        try:
            canonical = await self._read_owned_client(user, connection)
            if canonical is None:
                logger.error("Client %s not found for update", user.tg_id)
                return False
            current_device_limit = await self.get_limit_ip(user=user, client=canonical)
            if current_device_limit is None:
                return False
            if (duration <= 0 or devices < 0 or total_gb != 0
                    or (flow is not None and flow != canonical.flow)):
                return False
            # An update changes the shared client record. All attached
            # inbounds must support the preserved canonical flow. Never ignore
            # other memberships or normalize their shared credential/flow.
            for inbound_id in canonical.inbound_ids:
                inbound = await connection.adapter.get_inbound(inbound_id)
                self._validate_provisioning_capability(inbound, canonical.flow)

            if not replace_devices:
                devices = current_device_limit + devices

            current_time = get_current_timestamp()
            if canonical.expiry_time_ms <= 0:
                raise VPNReadError("Unlimited or delayed-start expiry requires manual review")
            if not replace_duration:
                expiry_time_to_use = max(canonical.expiry_time_ms, current_time)
            else:
                expiry_time_to_use = current_time

            expiry_time = add_days_to_timestamp(timestamp=expiry_time_to_use, days=duration)
            write = XUIClientWrite.from_client(
                canonical, expiry_time_ms=expiry_time,
                limit_ip=devices, enable=enable,
            )
            result = await connection.adapter.update_client(canonical, write)
            if result.node_pending is not False:
                raise XUIAmbiguousWriteError("Client persisted; node activation needs review")
            logger.info(f"Client {user.tg_id} updated successfully.")
            return True
        except XUIAmbiguousWriteError:
            raise
        except Exception as exception:
            logger.error("Error updating client %s (%s)", user.tg_id, type(exception).__name__)
            return False

    async def create_subscription(self, user: User, devices: int, duration: int) -> bool:
        return await self.create_client(user=user, devices=devices, duration=duration)

    async def extend_subscription(self, user: User, devices: int, duration: int) -> bool:
        return await self.update_client(
            user=user,
            devices=devices,
            duration=duration,
            replace_devices=True,
        )

    async def change_subscription(self, user: User, devices: int, duration: int) -> bool:
        if await self.is_client_exists(user):
            return await self.update_client(
                user,
                devices,
                duration,
                replace_devices=True,
                replace_duration=True,
            )
        return False

    async def process_bonus_days(self, user: User, duration: int, devices: int) -> bool:
        if await self.is_client_exists(user):
            updated = await self.update_client(user=user, devices=0, duration=duration)
            if updated:
                logger.info(f"Updated client {user.tg_id} with additional {duration} days(-s).")
                return True
        else:
            created = await self.create_client(user=user, devices=devices, duration=duration)
            if created:
                logger.info(f"Created client {user.tg_id} with additional {duration} days(-s)")
                return True

        return False

    async def activate_promocode(self, user: User, promocode: Promocode) -> bool:
        # TODO: consider moving to some 'promocode module services' with usage of vpn-service methods.

        async with self.session() as session:
            activated = await Promocode.set_activated(
                session=session,
                code=promocode.code,
                user_id=user.tg_id,
            )

        if not activated:
            logger.critical(f"Failed to activate promocode {promocode.code} for user {user.tg_id}.")
            return False

        logger.info(f"Begun applying promocode ({promocode.code}) to a client {user.tg_id}.")
        success = await self.process_bonus_days(
            user,
            duration=promocode.duration,
            devices=self.config.shop.BONUS_DEVICES_COUNT,
        )

        if success:
            return True

        async with self.session() as session:
            await Promocode.set_deactivated(session=session, code=promocode.code)

        logger.warning(f"Promocode {promocode.code} not activated due to failure.")
        return False
