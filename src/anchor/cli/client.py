"""Talking to the engine from a client (SPEC 5.1).

The command line holds no business logic: it turns arguments into requests,
sends them, and renders what comes back.
"""

from __future__ import annotations

import socket
from collections.abc import Callable, Iterator
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from anchor.protocol.messages import (
    MAX_LINE_BYTES,
    Event,
    Request,
    Response,
    decode_event,
    decode_response,
    encode,
)


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

    def subscribe(
        self,
        events: list[str] | None = None,
        *,
        should_stop: Callable[[], bool] = lambda: False,
    ) -> Iterator[Event]:
        """Turn this connection into an event feed and read it (SPEC 5.2).

        The engine answers the subscription first and then sends nothing but
        events, so this connection cannot be used for requests again. A quiet
        feed is normal — no session means no events — so a read timing out is
        not an error, only an opportunity to notice that we were asked to stop.
        """
        response = self.call("events.subscribe", {"events": events} if events else {})
        if not response.ok:
            raise EngineUnreachableError(
                f"the engine refused the subscription: {response.error.get('message')}"
            )

        if self._reader is None:  # pragma: no cover - call() would have raised
            raise EngineUnreachableError("not connected to the engine")

        while not should_stop():
            try:
                line = self._reader.readline(MAX_LINE_BYTES + 1)
            except TimeoutError:
                self._forget_the_timeout()
                continue
            except OSError as error:
                raise EngineUnreachableError(f"the event feed broke: {error}") from error
            if not line:
                raise EngineUnreachableError("the engine closed the event feed")
            yield decode_event(line)

    def _forget_the_timeout(self) -> None:
        """Let the feed keep reading after a quiet moment.

        Catching TimeoutError is not enough on its own. Python's socket file
        object records that a read timed out and then refuses every later read
        with "cannot read from timed out object" -- an OSError, which reads as
        a broken connection. The agent therefore dropped its feed the first
        time nothing happened for two seconds, reconnected, and did it again,
        for as long as it ran: a reconnection every two seconds forever, on an
        idle machine, which is exactly when nothing should be happening.

        The flag is cleared rather than the connection rebuilt because the
        buffer may already hold part of the next event, and reconnecting would
        throw it away along with the subscription. It is a private attribute
        of the standard library, so its absence is not an error: a Python that
        does not have it is a Python that does not need this.
        """
        raw: Any = getattr(self._reader, "raw", None)
        if raw is not None and getattr(raw, "_timeout_occurred", False):
            raw._timeout_occurred = False
