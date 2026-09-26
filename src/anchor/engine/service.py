"""The engine's IPC endpoint (SPEC 5.2).

Listens on ``/run/anchor/engine.sock`` and answers one JSON object per line.
Authorisation is the peer's own identity, read from the kernel with
``SO_PEERCRED``: root and the owner recorded at install time may give orders,
and nobody else. A client cannot claim a different identity, because it never
sends one, which is why no password is involved in normal use.
"""

from __future__ import annotations

import contextlib
import logging
import os
import pwd
import socket
import socketserver
import struct
import threading
from pathlib import Path as PathLike
from typing import Any

from anchor.engine.core import Engine
from anchor.protocol.errors import ProtocolError
from anchor.protocol.messages import (
    MAX_LINE_BYTES,
    Event,
    Request,
    Response,
    encode,
)

log = logging.getLogger("anchord")

#: ``struct ucred`` from the kernel: process, user and group identifiers.
_UCRED = struct.Struct("3i")

#: The kernel's limit on the path of a Unix socket, minus room for the
#: terminating byte. The installed path is far below it; a relocated tree used
#: for development can wander past it, and the kernel's own error does not say
#: which path it means.
MAX_SOCKET_PATH_BYTES = 107


class PeerIdentity:
    """Who is on the other end of a connection."""

    __slots__ = ("gid", "pid", "uid")

    def __init__(self, pid: int, uid: int, gid: int) -> None:
        self.pid = pid
        self.uid = uid
        self.gid = gid

    def __str__(self) -> str:
        return f"pid={self.pid} uid={self.uid}"


def peer_identity(connection: socket.socket) -> PeerIdentity:
    """Read the peer's credentials from the kernel (SPEC 5.2)."""
    raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _UCRED.size)
    pid, uid, gid = _UCRED.unpack(raw)
    return PeerIdentity(pid=pid, uid=uid, gid=gid)


class _Handler(socketserver.StreamRequestHandler):
    """One connection: a stream of requests, or a stream of events."""

    server: EngineServer

    def handle(self) -> None:
        self.server.remember(self.connection)
        try:
            self._handle()
        finally:
            self.server.forget(self.connection)

    def _handle(self) -> None:
        peer = peer_identity(self.connection)
        if not self.server.is_authorised(peer.uid):
            log.warning("rejected a connection from %s", peer)
            self._send(
                Response(
                    id="unauthorised",
                    ok=False,
                    error={
                        "code": "UNAUTHORIZED",
                        "message": "only root and the Anchor owner may give orders",
                    },
                )
            )
            return

        while True:
            line = self.rfile.readline(MAX_LINE_BYTES + 1)
            if not line:
                break
            if len(line) > MAX_LINE_BYTES:
                self._send(
                    Response(
                        id="oversized",
                        ok=False,
                        error={"code": "BAD_REQUEST", "message": "message is too long"},
                    )
                )
                break

            try:
                request = Request.from_dict(_parse(line))
            except ProtocolError as error:
                self._send(Response(id="malformed", ok=False, error=error.as_payload()))
                continue

            if request.type == "events.subscribe":
                self._send(request.ok({"subscribed": True}))
                self._stream_events()
                break

            self._send(self.server.serve(request))

    def _stream_events(self) -> None:
        """Turn this connection into a one-way event feed."""
        outbox: list[Event] = []
        wakeup = threading.Event()

        def sink(event: Event) -> None:
            outbox.append(event)
            wakeup.set()

        self.server.engine.subscribe(sink)
        try:
            while True:
                wakeup.wait(timeout=1.0)
                wakeup.clear()
                while outbox:
                    self._send(outbox.pop(0))
        except OSError:
            pass  # The subscriber hung up.
        finally:
            self.server.engine.unsubscribe(sink)

    def _send(self, message: Request | Response | Event) -> None:
        self.wfile.write(encode(message))
        self.wfile.flush()


def _parse(line: bytes) -> dict[str, Any]:
    from anchor.protocol.messages import decode_json_line

    return decode_json_line(line)


class EngineServer(socketserver.ThreadingUnixStreamServer):
    """A threaded server guarding a single engine."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._lock = threading.Lock()
        # Every open client connection, so that server_close can hang up.
        self._connections: set[socket.socket] = set()

        path = engine.paths.engine_socket
        encoded = str(path).encode("utf-8")
        if len(encoded) > MAX_SOCKET_PATH_BYTES:
            raise OSError(
                f"the socket path is {len(encoded)} bytes, and the kernel allows "
                f"{MAX_SOCKET_PATH_BYTES}: {path}. Anchor installs it at "
                "/run/anchor/engine.sock; a shorter --root is needed here."
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
        super().__init__(str(path), _Handler)
        self._restrict(path)

    def _restrict(self, path: PathLike) -> None:
        """Let the owner reach the socket, and nobody else who does not need to.

        ``SO_PEERCRED`` is the real check; these permissions only keep
        unrelated processes from connecting at all.
        """
        try:
            owner = pwd.getpwuid(self.engine.settings.owner_uid)
            os.chown(path, 0, owner.pw_gid)
            path.chmod(0o660)
        except (KeyError, PermissionError, OSError):
            # Running unprivileged, in development or a test: the credential
            # check still applies, so fall back to a reachable socket.
            path.chmod(0o666)

    def server_close(self) -> None:
        """Close the listening socket, and hang up on the subscribers.

        A subscriber sits on an open connection for as long as it lives, so
        shutting the server down without closing them leaves every client
        believing the engine is still there: no error, no end of stream, just
        an engine that has stopped answering and a client that will never ask.
        A dying process gets this for free from the kernel; an orderly
        shutdown has to do it itself.
        """
        with self._lock:
            open_now = list(self._connections)
            self._connections.clear()
        for connection in open_now:
            # Already gone is the outcome this is asking for.
            with contextlib.suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)
        super().server_close()

    def remember(self, connection: socket.socket) -> None:
        with self._lock:
            self._connections.add(connection)

    def forget(self, connection: socket.socket) -> None:
        with self._lock:
            self._connections.discard(connection)

    def is_authorised(self, uid: int) -> bool:
        """Accept root and the owner recorded at install time (SPEC 5.2)."""
        return uid == 0 or uid == self.engine.settings.owner_uid

    def serve(self, request: Request) -> Response:
        """Answer one request, one at a time."""
        with self._lock:
            return self.engine.handle(request)

    def tick(self) -> None:
        with self._lock:
            self.engine.tick()
            status = self.engine.status()
        if status.get("active"):
            self.engine.emit("session.tick", status)
