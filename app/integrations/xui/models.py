"""Small read models for the 3x-ui v3.8.5 API; independent of py3xui."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .exceptions import XUIAuthorizationError, XUIProtocolError


class XUIAuthMode(str, Enum):
    SESSION = "session"
    TOKEN = "token"


def _obj(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise XUIProtocolError(f"Invalid {name} object")
    return value


def _str(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise XUIProtocolError(f"Invalid {key} field")
    return value


def _int(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    if type(value) is not int:
        raise XUIProtocolError(f"Invalid {key} field")
    return value


def _bool(data: dict[str, Any], key: str) -> bool:
    value = data.get(key)
    if type(value) is not bool:
        raise XUIProtocolError(f"Invalid {key} field")
    return value


@dataclass(frozen=True, slots=True)
class XUIServerStatus:
    panel_version: str
    xray_version: str
    raw: dict[str, Any] = field(repr=False)

    @classmethod
    def from_api(cls, value: Any) -> XUIServerStatus:
        data = _obj(value, "server status")
        xray = _obj(data.get("xray"), "xray")
        return cls(_str(data, "panelVersion"), _str(xray, "version"), data.copy())


@dataclass(frozen=True, slots=True)
class XUIInboundSummary:
    id: int
    protocol: str
    enable: bool
    remark: str
    tag: str
    stream_settings: dict[str, Any] | None = field(repr=False)
    raw: dict[str, Any] = field(repr=False)

    @classmethod
    def from_api(cls, value: Any) -> XUIInboundSummary:
        data = _obj(value, "inbound")
        inbound_id = _int(data, "id")
        if inbound_id <= 0:
            raise XUIProtocolError("Invalid inbound ID")
        settings = data.get("streamSettings")
        if settings is not None:
            settings = _obj(settings, "streamSettings")
        return cls(inbound_id, _str(data, "protocol"), _bool(data, "enable"),
                   _str(data, "remark"), _str(data, "tag"), settings, data.copy())


@dataclass(frozen=True, slots=True)
class XUIClient:
    record_id: int
    email: str
    uuid: str = field(repr=False)
    enable: bool
    expiry_time_ms: int
    total_bytes: int
    limit_ip: int
    limit_hwid: int
    tg_id: int
    sub_id: str = field(repr=False)
    flow: str
    comment: str
    reset: int
    inbound_ids: tuple[int, ...]
    raw: dict[str, Any] = field(repr=False)

    @classmethod
    def from_api(cls, value: Any) -> XUIClient:
        wrapper = _obj(value, "client response")
        data = _obj(wrapper.get("client"), "canonical client")
        inbound_ids = wrapper.get("inboundIds")
        if not isinstance(inbound_ids, list) or any(type(i) is not int or i <= 0 for i in inbound_ids):
            raise XUIProtocolError("Invalid inboundIds field")
        if len(set(inbound_ids)) != len(inbound_ids):
            raise XUIProtocolError("Duplicate inbound ID")
        return cls(
            record_id=_int(data, "id"), email=_str(data, "email"),
            uuid=_str(data, "uuid"), enable=_bool(data, "enable"),
            expiry_time_ms=_int(data, "expiryTime"), total_bytes=_int(data, "totalGB"),
            limit_ip=_int(data, "limitIp"), limit_hwid=_int(data, "limitHwid"),
            tg_id=_int(data, "tgId"), sub_id=_str(data, "subId"),
            flow=_str(data, "flow"), comment=_str(data, "comment"),
            reset=_int(data, "reset"), inbound_ids=tuple(inbound_ids), raw=data.copy(),
        )


@dataclass(frozen=True, slots=True)
class XUIClientTraffic:
    id: int  # Numeric traffic row ID; never a client credential UUID.
    email: str
    up: int
    down: int
    total: int
    expiry_time_ms: int
    enable: bool
    uuid: str | None = field(repr=False)
    inbound_id: int
    raw: dict[str, Any] = field(repr=False)

    @classmethod
    def from_api(cls, value: Any) -> XUIClientTraffic:
        data = _obj(value, "client traffic")
        uuid = data.get("uuid")
        if uuid is not None and not isinstance(uuid, str):
            raise XUIProtocolError("Invalid traffic uuid field")
        return cls(
            id=_int(data, "id"), email=_str(data, "email"),
            up=_int(data, "up"), down=_int(data, "down"), total=_int(data, "total"),
            expiry_time_ms=_int(data, "expiryTime"), enable=_bool(data, "enable"),
            uuid=uuid, inbound_id=_int(data, "inboundId"), raw=data.copy(),
        )


def is_member_of(client: XUIClient, inbound_id: int) -> bool:
    """Check membership in a caller-chosen inbound without selecting one."""
    if type(inbound_id) is not int or inbound_id <= 0:
        raise ValueError("configured inbound ID must be a positive integer")
    return inbound_id in client.inbound_ids


def validate_client_membership(client: XUIClient, configured_inbound_id: int) -> None:
    """Fail closed if the canonical client is absent from the configured inbound."""
    if not is_member_of(client, configured_inbound_id):
        raise XUIAuthorizationError("Client is not in the configured inbound")
