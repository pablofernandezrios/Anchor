"""IPC envelopes and the registry of message types (SPEC 5.2).

Transport is ``AF_UNIX``/``SOCK_STREAM`` carrying one JSON object per line in
UTF-8. Three envelopes exist:

* request  ``{"v":1,"id":"<uuid>","type":"session.start","payload":{...}}``
* response ``{"v":1,"id":"<same>","ok":true,"result":{...}}`` or
           ``{"v":1,"id":"<same>","ok":false,"error":{"code","message"}}``
* event    ``{"v":1,"event":"session.tick","payload":{...}}``

Every order receives an explicit acknowledgement; nothing is fire-and-forget.

Only message types whose payload is settled by the specification are registered
here. The profile, schedule and category editing commands in SPEC 15 arrive
with the milestones that define their data model, so that their schemas are
written once rather than guessed now.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Self

from anchor.protocol.errors import ErrorCode, ProtocolError
from anchor.protocol.schema import EMPTY, Field, Schema
from anchor.protocol.types import Level, SessionOrigin, Valve

PROTOCOL_VERSION: Final = 1

#: Longest accepted line. A client that sends more is misbehaving, and the
#: engine should not grow its memory on demand.
MAX_LINE_BYTES: Final = 1 << 20

#: The 8-hour cap applies to manual sessions at start (SPEC 7.1).
MAX_MANUAL_DURATION_SECONDS: Final = 8 * 60 * 60

_LEVELS: Final = tuple(str(level) for level in Level)
_VALVES: Final = tuple(str(valve) for valve in Valve)
_ORIGINS: Final = tuple(str(origin) for origin in SessionOrigin)


REQUEST_SCHEMAS: Final[dict[str, Schema]] = {
    "status.get": EMPTY,
    "session.start": Schema(
        profile=Field(str),
        duration_seconds=Field(int, minimum=1),
        level=Field(str, choices=_LEVELS, convert=Level),
        valve=Field(str, required=False, default=None, choices=_VALVES, convert=Valve),
        origin=Field(
            str,
            required=False,
            default=SessionOrigin.MANUAL,
            choices=_ORIGINS,
            convert=SessionOrigin,
        ),
    ),
    "session.extend": Schema(by_seconds=Field(int, minimum=1)),
    "session.cancel": Schema(typed=Field(str, required=False, default=None)),
    "session.withdraw_cancel": EMPTY,
    "valve.request": EMPTY,
    "valve.withdraw": EMPTY,
    "valve.phrase": Schema(text=Field(str)),
    "schedule.skip": EMPTY,
    "stats.query": Schema(
        range=Field(str, choices=("day", "week", "month"), required=False, default="week"),
    ),
    "config.get": Schema(key=Field(str, required=False, default=None)),
    "config.set": Schema(key=Field(str), value=Field(str)),
    "doctor.run": EMPTY,
    "events.subscribe": Schema(
        events=Field(list, required=False, default=None, item_kind=str),
    ),
}

#: Events the engine publishes to subscribers.
EVENT_TYPES: Final = frozenset(
    {
        "session.started",
        "session.tick",
        "session.ended",
        "session.extended",
        "break.warning",
        "break.started",
        "break.ended",
        "valve.requested",
        "valve.withdrawn",
        "rupture.recorded",
        "blocked.attempt",
        "config.changed",
    }
)


def new_id() -> str:
    """Return a fresh request identifier."""
    return str(uuid.uuid4())


def _require_version(raw: Mapping[str, Any]) -> None:
    if "v" not in raw:
        raise ProtocolError("message is missing the protocol version 'v'")
    version = raw["v"]
    if not isinstance(version, int) or isinstance(version, bool):
        raise ProtocolError("protocol version 'v' must be an integer")
    if version != PROTOCOL_VERSION:
        raise ProtocolError(
            f"unsupported protocol version {version}; this build speaks {PROTOCOL_VERSION}",
            code=ErrorCode.UNSUPPORTED_VERSION,
        )


def _require_keys(raw: Mapping[str, Any], allowed: frozenset[str], kind: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        names = ", ".join(repr(key) for key in unknown)
        raise ProtocolError(f"{kind} has unknown fields: {names}")


def _require_id(raw: Mapping[str, Any]) -> str:
    message_id = raw.get("id")
    if not isinstance(message_id, str) or not message_id:
        raise ProtocolError("field 'id' must be a non-empty string")
    if len(message_id) > 128:
        raise ProtocolError("field 'id' is too long")
    return message_id


@dataclass(frozen=True, slots=True)
class Request:
    """An order from a client to the engine."""

    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=new_id)

    _KEYS = frozenset({"v", "id", "type", "payload"})

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> Self:
        if not isinstance(raw, Mapping):
            raise ProtocolError("a request must be a JSON object")
        _require_version(raw)
        _require_keys(raw, cls._KEYS, "request")
        message_id = _require_id(raw)

        message_type = raw.get("type")
        if not isinstance(message_type, str) or not message_type:
            raise ProtocolError("field 'type' must be a non-empty string")

        schema = REQUEST_SCHEMAS.get(message_type)
        if schema is None:
            raise ProtocolError(
                f"unknown request type {message_type!r}", code=ErrorCode.UNKNOWN_TYPE
            )

        payload = raw.get("payload", {})
        if payload is None:
            payload = {}
        validated = schema.validate(payload, context=f"payload of {message_type!r}")
        return cls(type=message_type, payload=validated, id=message_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "v": PROTOCOL_VERSION,
            "id": self.id,
            "type": self.type,
            "payload": _jsonable(self.payload),
        }

    def ok(self, result: Mapping[str, Any] | None = None) -> Response:
        return Response(id=self.id, ok=True, result=dict(result or {}))

    def fail(self, code: ErrorCode, message: str) -> Response:
        return Response(id=self.id, ok=False, error={"code": str(code), "message": message})


@dataclass(frozen=True, slots=True)
class Response:
    """The engine's explicit acknowledgement of a request."""

    id: str
    ok: bool
    result: dict[str, Any] = field(default_factory=dict)
    error: dict[str, str] = field(default_factory=dict)

    _KEYS = frozenset({"v", "id", "ok", "result", "error"})

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> Self:
        if not isinstance(raw, Mapping):
            raise ProtocolError("a response must be a JSON object")
        _require_version(raw)
        _require_keys(raw, cls._KEYS, "response")
        message_id = _require_id(raw)

        ok = raw.get("ok")
        if not isinstance(ok, bool):
            raise ProtocolError("field 'ok' must be a boolean")

        if ok:
            result = raw.get("result", {})
            if not isinstance(result, Mapping):
                raise ProtocolError("field 'result' must be an object")
            return cls(id=message_id, ok=True, result=dict(result))

        error = raw.get("error")
        if not isinstance(error, Mapping):
            raise ProtocolError("a failed response must carry an 'error' object")
        code = error.get("code")
        message = error.get("message")
        if not isinstance(code, str) or not code:
            raise ProtocolError("field 'error.code' must be a non-empty string")
        if not isinstance(message, str):
            raise ProtocolError("field 'error.message' must be a string")
        return cls(id=message_id, ok=False, error={"code": code, "message": message})

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {"v": PROTOCOL_VERSION, "id": self.id, "ok": self.ok}
        if self.ok:
            body["result"] = _jsonable(self.result)
        else:
            body["error"] = dict(self.error)
        return body

    @property
    def code(self) -> str | None:
        """The error code of a failed response, if any."""
        return self.error.get("code") if not self.ok else None


@dataclass(frozen=True, slots=True)
class Event:
    """A one-way notification from the engine to its subscribers."""

    event: str
    payload: dict[str, Any] = field(default_factory=dict)

    _KEYS = frozenset({"v", "event", "payload"})

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> Self:
        if not isinstance(raw, Mapping):
            raise ProtocolError("an event must be a JSON object")
        _require_version(raw)
        _require_keys(raw, cls._KEYS, "event")

        name = raw.get("event")
        if not isinstance(name, str) or not name:
            raise ProtocolError("field 'event' must be a non-empty string")
        if name not in EVENT_TYPES:
            raise ProtocolError(f"unknown event {name!r}", code=ErrorCode.UNKNOWN_TYPE)

        payload = raw.get("payload", {})
        if payload is None:
            payload = {}
        if not isinstance(payload, Mapping):
            raise ProtocolError("field 'payload' must be an object")
        return cls(event=name, payload=dict(payload))

    def to_dict(self) -> dict[str, Any]:
        return {
            "v": PROTOCOL_VERSION,
            "event": self.event,
            "payload": _jsonable(self.payload),
        }


def _jsonable(value: Any) -> Any:
    """Convert enums and nested containers into plain JSON values."""
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, Level | Valve | SessionOrigin):
        return str(value)
    return value


def encode(message: Request | Response | Event) -> bytes:
    """Serialise ``message`` as one newline-terminated UTF-8 line."""
    line = json.dumps(message.to_dict(), separators=(",", ":"), ensure_ascii=False)
    data = line.encode("utf-8") + b"\n"
    if len(data) > MAX_LINE_BYTES:
        raise ProtocolError("message is too large to send")
    return data


def decode_json_line(line: bytes | str) -> dict[str, Any]:
    """Parse one wire line into a JSON object, with protocol-shaped errors."""
    if isinstance(line, bytes):
        if len(line) > MAX_LINE_BYTES:
            raise ProtocolError("message exceeds the maximum line length")
        try:
            line = line.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolError("message is not valid UTF-8") from exc
    try:
        parsed = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"message is not valid JSON: {exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise ProtocolError("message must be a JSON object")
    return parsed


def decode_request(line: bytes | str) -> Request:
    return Request.from_dict(decode_json_line(line))


def decode_response(line: bytes | str) -> Response:
    return Response.from_dict(decode_json_line(line))
