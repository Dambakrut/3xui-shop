import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Config
from app.db.models import Server, User
from app.integrations.xui import XUIAdapter, XUIAuthMode, XUIInboundSummary


KNOWN_PROTOCOLS = frozenset({
    "vmess", "vless", "trojan", "shadowsocks", "wireguard", "hysteria",
    "http", "mixed", "tunnel", "tun", "mtproto", "amneziawg", "tuic",
})
TARGET_PANEL_VERSION = "3.8.5"

logger = logging.getLogger(__name__)


@dataclass
class Connection:
    server: Server
    adapter: XUIAdapter


class ServerPoolService:
    def __init__(self, config: Config, session: async_sessionmaker) -> None:
        self.config = config
        self.session = session
        self._servers: dict[int, Connection] = {}
        logger.info("Server Pool Service initialized.")

    async def _add_server(self, server: Server) -> None:
        if server.id not in self._servers:
            adapter = None
            try:
                mode = XUIAuthMode(getattr(self.config.xui, "AUTH_MODE", "session"))
                adapter = XUIAdapter(
                    server.host,
                    auth_mode=mode,
                    username=self.config.xui.USERNAME if mode is XUIAuthMode.SESSION else None,
                    password=self.config.xui.PASSWORD if mode is XUIAuthMode.SESSION else None,
                    token=getattr(self.config.xui, "API_TOKEN", None) if mode is XUIAuthMode.TOKEN else None,
                )
                await adapter.authenticate()
                status = await adapter.get_server_status()
                if status.panel_version.removeprefix("v") != TARGET_PANEL_VERSION:
                    logger.error("Server %s has unsupported panel version %r", server.id, status.panel_version)
                    server.online = False
                elif status.xray_state != "running":
                    logger.error("Server %s reports Xray state %r", server.id, status.xray_state)
                    server.online = False
                else:
                    server.online = await self.validate_configured_inbound(adapter) is not None
                if server.online:
                    self._servers[server.id] = Connection(server=server, adapter=adapter)
                    logger.info("Server %s available for panel operations", server.id)
            except Exception as exception:
                server.online = False
                logger.error("Server %s unavailable for reads (%s)", server.id, type(exception).__name__)
            finally:
                if adapter is not None and server.id not in self._servers:
                    await adapter.close()

            async with self.session() as session:
                await Server.update(session=session, name=server.name, online=server.online)

    async def _remove_server(self, server: Server) -> None:
        connection = self._servers.pop(server.id, None)
        if connection is not None:
            await connection.adapter.close()

    async def close(self) -> None:
        """Release all per-server aiohttp sessions on shutdown or startup failure."""
        connections = list(self._servers.values())
        self._servers.clear()
        for connection in connections:
            await connection.adapter.close()

    async def refresh_server(self, server: Server) -> None:
        if server.id in self._servers:
            await self._remove_server(server)

        await self._add_server(server)
        logger.info("Server %s refresh finished", server.id)

    async def validate_configured_inbound(self, adapter: XUIAdapter) -> XUIInboundSummary | None:
        try:
            inbounds = await adapter.list_inbounds()
        except Exception as exception:
            logger.error("Inbound validation failed for server adapter (%s)", type(exception).__name__)
            return None
        if not isinstance(inbounds, (list, tuple)):
            logger.error("Inbound validation received a malformed list")
            return None
        for inbound in inbounds:
            if inbound.id != self.config.xui.INBOUND_ID:
                continue
            if not inbound.enable:
                logger.error("Configured inbound %s is disabled", inbound.id)
                return None
            if inbound.protocol not in KNOWN_PROTOCOLS:
                logger.error("Configured inbound %s has unknown protocol %r", inbound.id, inbound.protocol)
                return None
            logger.info(
                "Validated inbound id=%s protocol=%r remark=%r",
                inbound.id, inbound.protocol, inbound.remark[:80],
            )
            return inbound
        logger.error("Configured inbound %s is absent", self.config.xui.INBOUND_ID)
        return None

    async def get_connection(self, user: User) -> Connection | None:
        if not user.server_id:
            logger.debug(f"User {user.tg_id} not assigned to any server.")
            return None

        connection = self._servers.get(user.server_id)

        if not connection:
            available_servers = list(self._servers.keys())
            logger.critical(
                f"Server {user.server_id} not found in pool. "
                f"User assigned server: {user.server_id}, "
                f"Available servers in pool: {available_servers}"
            )

            async with self.session() as session:
                server = await Server.get_by_id(session=session, id=user.server_id)

            if server:
                logger.debug(f"Server {server.name} ({server.host}) found in database.")
                # TODO: Try to add server to pool
            else:
                logger.error(f"Server {user.server_id} not found in database.")

            return None

        async with self.session() as session:
            server = await Server.get_by_id(session=session, id=user.server_id)

        connection.server = server
        return connection

    async def sync_servers(self) -> None:
        async with self.session() as session:
            db_servers = await Server.get_all(session)

        if not db_servers and not self._servers:
            logger.warning("No servers found in the database.")
            return

        db_server_map = {server.id: server for server in db_servers}

        for server_id in list(self._servers.keys()):
            if server_id not in db_server_map:
                await self._remove_server(self._servers[server_id].server)

        refreshed_ids = set()
        for server_id, conn in list(self._servers.items()):
            if db_server := db_server_map.get(server_id):
                conn.server = db_server
            await self.refresh_server(conn.server)
            refreshed_ids.add(server_id)

        for server in db_servers:
            if server.id not in self._servers and server.id not in refreshed_ids:
                await self._add_server(server)

        logger.info(f"Sync complete. Currently active servers: {len(self._servers)}")

    async def assign_server_to_user(self, user: User, server: Server) -> bool:
        async with self.session() as session:
            updated = await User.update(session=session, tg_id=user.tg_id, server_id=server.id)
        if not updated:
            logger.error(f"Failed to assign server {server.id} to user {user.tg_id} after client creation.")
            return False
        user.server_id = server.id
        return True

    async def get_provisioning_connection(self) -> Connection | None:
        server = await self.get_available_server()
        if not server:
            return None
        return self._servers.get(server.id)

    async def get_available_server(self) -> Server | None:
        await self.sync_servers()

        servers_with_free_slots = [
            conn.server
            for conn in self._servers.values()
            if conn.server.current_clients < conn.server.max_clients
        ]

        if servers_with_free_slots:
            server = sorted(servers_with_free_slots, key=lambda s: s.current_clients)[0]
            logger.debug(
                f"Found server with free slots: {server.name} "
                f"(clients: {server.current_clients}/{server.max_clients})"
            )
            return server

        servers_least_loaded = [conn.server for conn in self._servers.values()]
        if servers_least_loaded:
            server = sorted(servers_least_loaded, key=lambda s: s.current_clients)[0]
            logger.warning(
                f"No servers with free slots. Using least loaded server: {server.name} "
                f"(clients: {server.current_clients}/{server.max_clients})"
            )
            return server

        logger.critical("No available servers found in pool")
        return None
