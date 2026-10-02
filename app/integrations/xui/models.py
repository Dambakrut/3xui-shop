"""Typed models for the 3x-ui v3.8.5 API; independent of py3xui."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import json
from copy import deepcopy
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
    xray_state: str
    raw: dict[str, Any] = field(repr=False)

    @classmethod
    def from_api(cls, value: Any) -> XUIServerStatus:
        data = _obj(value, "server status")
        xray = _obj(data.get("xray"), "xray")
        return cls(_str(data, "panelVersion"), _str(xray, "version"),
                   _str(xray, "state"), data.copy())


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


# The Go update endpoint decodes a full model.Client, not a PATCH. Only fields
# declared by that model are round-tripped; record IDs and server timestamps
# must never be sent back. Typed identity always overrides raw values.
CLIENT_WRITE_FIELDS = frozenset({
    "security", "password", "reverse", "auth", "privateKey",
    "publicKey", "allowedIPs", "preSharedKey", "keepAlive",
    "forwardedPorts", "secret", "adTag", "group",
    "resetDay", "resetMax", "trafficReset", "trafficResetDay",
})
CLIENT_WRITE_DEFAULTS = {
    "security": "auto", "password": "", "reverse": None, "auth": "",
    "privateKey": "", "publicKey": "", "allowedIPs": [], "preSharedKey": "",
    "keepAlive": 0, "forwardedPorts": "", "secret": "", "adTag": "", "group": "",
    "resetDay": 0, "resetMax": 0, "trafficReset": "never", "trafficResetDay": 1,
}


@dataclass(frozen=True, slots=True)
class XUIClientWrite:
    email: str
    uuid: str = field(repr=False)
    inbound_ids: tuple[int, ...]
    expiry_time_ms: int
    total_bytes: int
    limit_ip: int
    limit_hwid: int
    tg_id: int
    sub_id: str = field(repr=False)
    enable: bool = True
    flow: str = ""
    comment: str = ""
    reset: int = 0
    preserved: dict[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if not self.email or not self.uuid or not self.sub_id:
            raise ValueError("Client email, UUID and subId are required")
        if not self.inbound_ids or any(type(i) is not int or i <= 0 for i in self.inbound_ids):
            raise ValueError("Positive inbound IDs are required")
        if len(set(self.inbound_ids)) != len(self.inbound_ids):
            raise ValueError("Duplicate inbound IDs")
        if (type(self.expiry_time_ms) is not int or self.expiry_time_ms <= 0
                or any(type(x) is not int or x < 0 for x in
                       (self.total_bytes, self.limit_ip, self.limit_hwid, self.tg_id))
                or type(self.enable) is not bool):
            raise ValueError("Invalid client write fields")
        if set(self.preserved) - CLIENT_WRITE_FIELDS:
            raise ValueError("Unsupported preserved client field")
        for key, value in self.preserved.items():
            if key == "reverse":
                valid = value is None or (isinstance(value, dict)
                         and set(value) == {"tag"} and isinstance(value["tag"], str))
            elif key == "allowedIPs":
                valid = isinstance(value, list) and all(isinstance(i, str) for i in value)
            else:
                valid = type(value) is type(CLIENT_WRITE_DEFAULTS[key])
            if not valid:
                raise XUIProtocolError("Invalid preserved client field")
        object.__setattr__(self, "preserved", deepcopy(self.preserved))
        if (not isinstance(self.flow, str) or not isinstance(self.comment, str)
                or type(self.reset) is not int):
            raise ValueError("Invalid client metadata")
        for value in (self.email, self.sub_id):
            if any(c.isspace() or ord(c) < 32 or c in "/\\" for c in value):
                raise ValueError("Invalid client identifier")

    @classmethod
    def from_client(cls, client: XUIClient, *, expiry_time_ms: int,
                    limit_ip: int, enable: bool) -> XUIClientWrite:
        if CLIENT_WRITE_FIELDS - client.raw.keys():
            raise XUIProtocolError("Canonical client lacks preservation fields")
        preserved = deepcopy({key: client.raw[key] for key in CLIENT_WRITE_FIELDS if key in client.raw})
        # ClientRecord stores this as JSON text; model.Client accepts []string.
        if "allowedIPs" in preserved:
            value = preserved["allowedIPs"]
            if isinstance(value, str):
                try:
                    value = json.loads(value) if value else []
                except ValueError as exc:
                    raise XUIProtocolError("Invalid canonical allowedIPs") from exc
            if value is None:
                value = []
            if not isinstance(value, list) or any(not isinstance(i, str) for i in value):
                raise XUIProtocolError("Invalid canonical allowedIPs")
            preserved["allowedIPs"] = value
        return cls(
            email=client.email, uuid=client.uuid, inbound_ids=client.inbound_ids,
            expiry_time_ms=expiry_time_ms, total_bytes=client.total_bytes,
            limit_ip=limit_ip, limit_hwid=client.limit_hwid, tg_id=client.tg_id,
            sub_id=client.sub_id, enable=enable,
            flow=client.flow, comment=client.comment, reset=client.reset,
            preserved=preserved,
        )

    def client_payload(self) -> dict[str, Any]:
        payload = deepcopy(CLIENT_WRITE_DEFAULTS)
        payload.update(deepcopy(self.preserved))
        payload.update({
            "id": self.uuid, "email": self.email, "enable": self.enable,
            "expiryTime": self.expiry_time_ms, "totalGB": self.total_bytes,
            "limitIp": self.limit_ip, "limitHwid": self.limit_hwid,
            "tgId": self.tg_id, "subId": self.sub_id,
            "flow": self.flow, "comment": self.comment, "reset": self.reset,
        })
        return payload


@dataclass(frozen=True, slots=True)
class XUIWriteResult:
    client: XUIClient = field(repr=False)
    # False/True only from an explicit mutation response nodePending boolean.
    # None: persistence confirmed, but node activation is unknown.
    node_pending: bool | None
    reconciled: bool
    success: bool = True  # Canonical persistence verified, not node activation.


def is_member_of(client: XUIClient, inbound_id: int) -> bool:
    """Check membership in a caller-chosen inbound without selecting one."""
    if type(inbound_id) is not int or inbound_id <= 0:
        raise ValueError("configured inbound ID must be a positive integer")
    return inbound_id in client.inbound_ids


def validate_client_membership(client: XUIClient, configured_inbound_id: int) -> None:
    """Fail closed if the canonical client is absent from the configured inbound."""
    if not is_member_of(client, configured_inbound_id):
        raise XUIAuthorizationError("Client is not in the configured inbound")
