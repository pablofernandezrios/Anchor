"""Talking to the engine from a client (SPEC 5.1).

The command line holds no business logic: it turns arguments into requests,
sends them, and renders what comes back.
"""

from __future__ import annotations

import socket
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from anchor.protocol.messages import MAX_LINE_BYTES, Request, Response, decode_response, encode


class EngineUnreachableError(RuntimeError):
    """The engine socket is not there, or will not accept us."""


class EngineClient:
    """One short-lived connection to the engine."""

    def __init__(self, socket_path: Path, *, timeout: float = 10.0) -> None:
        self._path = socket_path
        self._timeout = timeout
        self._socket: socket.socket | None = None
        self._reader: Any = None

    def __enter__(self) -> Self:
        self.connect()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def connect(self) -> None:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self._timeout)
        try:
            connection.connect(str(self._path))
        except FileNotFoundError as error:
            connection.close()
            raise EngineUnreachableError(
                f"the engine is not running: {self._path} does not exist. "
                "Try: systemctl status anchord"
            ) from error
        except PermissionError as error:
            connection.close()
            raise EngineUnreachableError(
                f"not allowed to reach {self._path}. Anchor accepts orders from root "
                "and from the user recorded when it was installed."
            ) from error
        except OSError as error:
            connection.close()
            raise EngineUnreachableError(f"cannot reach the engine: {error}") from error

        self._socket = connection
        self._reader = connection.makefile("rb")

    def close(self) -> None:
        if self._reader is not None:
            self._reader.close()
            self._reader = None
        if self._socket is not None:
            self._socket.close()
            self._socket = None

    def call(self, type_: str, payload: dict[str, Any] | None = None) -> Response:
        """Send one request and wait for its acknowledgement."""
        if self._socket is None or self._reader is None:
            raise EngineUnreachableError("not connected to the engine")

        request = Request(type=type_, payload=payload or {})
        self._socket.sendall(encode(request))

        line = self._reader.readline(MAX_LINE_BYTES + 1)
        if not line:
            raise EngineUnreachableError("the engine closed the connection without answering")
        return decode_response(line)
