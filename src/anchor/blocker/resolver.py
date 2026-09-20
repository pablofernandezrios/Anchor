"""Anchor's forwarding DNS resolver (SPEC 8.2).

Every DNS query on the machine is redirected here by nftables. The resolver
reads the question, and either refuses the name or passes the original bytes
upstream untouched and hands the reply straight back. It rewrites nothing.

Three things are load-bearing:

**The mark.** Upstream sockets carry ``SO_MARK``, and the nftables rule returns
early on marked packets, so the resolver's own queries are not redirected back
into itself. Milestone 0 spike 2 established that setting the mark needs
``CAP_NET_ADMIN``, which is why an ordinary process cannot claim the exemption
by pretending to be the resolver.

**No answer parsing.** A reply is forwarded as bytes. The less of a remote
server's packet Anchor interprets, the less there is to get wrong. The map of
recent answers that SPEC 8.2 needs for dropping open connections arrives with
that feature, and will parse only the address records it needs.

**No domain ever reaches the log.** The working rules forbid logging visited
domains outside the statistics database, so blocked names go to a callback the
engine turns into statistics and notifications, and never to ``journalctl``.
Failures are logged by what went wrong, not by what was asked for.
"""

from __future__ import annotations

import logging
import socket
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Final

from anchor.blocker.constants import ANCHOR_MARK, RESOLVER_ADDRESS, RESOLVER_PORT
from anchor.blocker.dnswire import (
    MalformedMessageError,
    is_query,
    nxdomain_response,
    question_name,
    servfail_response,
)
from anchor.blocker.matcher import Policy

log = logging.getLogger("anchor-blockerd")

#: Largest DNS message Anchor will handle. Well above the 4096 an EDNS client
#: advertises, and far below anything worth a memory concern.
MAX_MESSAGE: Final = 8192

#: How long to wait for one upstream before trying the next.
UPSTREAM_TIMEOUT: Final = 3.0

#: How long a TCP client may dawdle before its connection is dropped.
CLIENT_TIMEOUT: Final = 5.0

#: Called with the name and the rule that blocked it.
BlockedHandler = Callable[[str, str], None]

#: Returns the policy in force, or ``None`` when no session is running.
PolicyProvider = Callable[[], Policy | None]


@dataclass(slots=True)
class ResolverConfig:
    """Where the resolver listens and where it forwards."""

    upstreams: list[str] = field(default_factory=list)
    address: str = RESOLVER_ADDRESS
    port: int = RESOLVER_PORT
    upstream_port: int = 53
    """Where the upstream servers listen. Only a test moves this off 53."""

    mark: int = ANCHOR_MARK
    timeout: float = UPSTREAM_TIMEOUT


class Resolver:
    """A forwarding resolver that refuses the names a session blocks."""

    def __init__(
        self,
        config: ResolverConfig,
        *,
        policy: PolicyProvider,
        on_blocked: BlockedHandler | None = None,
    ) -> None:
        self._config = config
        self._policy = policy
        self._on_blocked = on_blocked
        self._stopping = threading.Event()
        self._threads: list[threading.Thread] = []
        self._udp: socket.socket | None = None
        self._tcp: socket.socket | None = None
        self._upstream_lock = threading.Lock()

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        """Bind and begin serving. Returns once both sockets are listening."""
        self._udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._udp.bind((self._config.address, self._config.port))
        self._udp.settimeout(0.5)

        self._tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._tcp.bind((self._config.address, self._config.port))
        self._tcp.listen(32)
        self._tcp.settimeout(0.5)

        for target, name in (
            (self._serve_udp, "anchor-dns-udp"),
            (self._serve_tcp, "anchor-dns-tcp"),
        ):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)

        log.info(
            "resolver listening on %s:%d, forwarding to %s",
            self._config.address,
            self._config.port,
            ", ".join(self._config.upstreams) or "nothing yet",
        )

    def stop(self) -> None:
        self._stopping.set()
        for thread in self._threads:
            thread.join(timeout=3)
        for sock in (self._udp, self._tcp):
            if sock is not None:
                sock.close()
        self._udp = self._tcp = None
        self._threads.clear()

    def __enter__(self) -> Resolver:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    @property
    def port(self) -> int:
        """The port actually bound, which matters when the config said zero."""
        if self._udp is None:
            return self._config.port
        return int(self._udp.getsockname()[1])

    def set_upstreams(self, upstreams: Sequence[str]) -> None:
        """Point at a new set of servers, as the network changes (SPEC 8.2)."""
        with self._upstream_lock:
            self._config.upstreams = list(upstreams)
        log.info("resolver now forwarding to %s", ", ".join(upstreams) or "nothing")

    # -- serving ---------------------------------------------------------

    def _serve_udp(self) -> None:
        assert self._udp is not None
        while not self._stopping.is_set():
            try:
                packet, client = self._udp.recvfrom(MAX_MESSAGE)
            except TimeoutError:
                continue
            except OSError:
                break

            reply = self.handle(packet)
            if reply is None:
                continue
            try:
                self._udp.sendto(reply, client)
            except OSError:
                # The client gave up. Nothing to say about it, and saying it
                # would mean naming the query.
                continue

    def _serve_tcp(self) -> None:
        assert self._tcp is not None
        while not self._stopping.is_set():
            try:
                connection, _ = self._tcp.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            threading.Thread(
                target=self._handle_tcp_client,
                args=(connection,),
                name="anchor-dns-tcp-client",
                daemon=True,
            ).start()

    def _handle_tcp_client(self, connection: socket.socket) -> None:
        """Serve one TCP client. DNS over TCP prefixes each message with its length."""
        with connection:
            connection.settimeout(CLIENT_TIMEOUT)
            try:
                header = _recv_exactly(connection, 2)
                if header is None:
                    return
                length = int.from_bytes(header, "big")
                if length == 0 or length > MAX_MESSAGE:
                    return
                packet = _recv_exactly(connection, length)
                if packet is None:
                    return

                reply = self.handle(packet, over_tcp=True)
                if reply is None:
                    return
                connection.sendall(len(reply).to_bytes(2, "big") + reply)
            except OSError:
                return

    # -- the decision ----------------------------------------------------

    def handle(self, packet: bytes, *, over_tcp: bool = False) -> bytes | None:
        """Decide what to do with one query. Returns the bytes to send back."""
        if not is_query(packet):
            # A response arriving on the listening socket is not ours to relay.
            return None

        try:
            name = question_name(packet)
        except MalformedMessageError as error:
            # Refusing to interpret it is the whole point. Nothing is echoed
            # back, because building a reply needs the question we could not
            # read.
            log.debug("ignoring a malformed query: %s", error)
            return None

        policy = self._policy()
        rule = policy.matching_rule(name) if policy is not None else None

        if rule is not None:
            if self._on_blocked is not None:
                try:
                    self._on_blocked(name, rule)
                except Exception:
                    log.exception("the blocked-attempt handler raised")
            return nxdomain_response(packet)

        return self._forward(packet, over_tcp=over_tcp)

    # -- forwarding ------------------------------------------------------

    def _forward(self, packet: bytes, *, over_tcp: bool) -> bytes:
        with self._upstream_lock:
            upstreams = list(self._config.upstreams)

        for server in upstreams:
            reply = (
                self._forward_tcp(packet, server) if over_tcp else self._forward_udp(packet, server)
            )
            if reply is not None:
                return reply

        # Every upstream refused or timed out. Say so honestly rather than
        # claiming the name does not exist, which clients would cache.
        log.warning("no upstream answered (%d tried)", len(upstreams))
        return servfail_response(packet)

    def _mark(self, sock: socket.socket) -> None:
        """Mark the socket so the redirect lets it out (SPEC 8.2)."""
        if not self._config.mark:
            return
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_MARK, self._config.mark)
        except (OSError, AttributeError) as error:
            # Without CAP_NET_ADMIN the mark cannot be set. In a test that is
            # fine; on a real machine it means the resolver's own queries would
            # be redirected back into it, so it is worth a loud warning.
            log.warning(
                "could not mark the upstream socket (%s); on a machine with the "
                "redirect in place this would loop queries back into Anchor",
                error,
            )

    def _forward_udp(self, packet: bytes, server: str) -> bytes | None:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                self._mark(sock)
                sock.settimeout(self._config.timeout)
                sock.sendto(packet, (server, self._config.upstream_port))
                reply, _ = sock.recvfrom(MAX_MESSAGE)
        except OSError:
            return None
        return reply

    def _forward_tcp(self, packet: bytes, server: str) -> bytes | None:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                self._mark(sock)
                sock.settimeout(self._config.timeout)
                sock.connect((server, self._config.upstream_port))
                sock.sendall(len(packet).to_bytes(2, "big") + packet)

                header = _recv_exactly(sock, 2)
                if header is None:
                    return None
                length = int.from_bytes(header, "big")
                if length == 0 or length > MAX_MESSAGE:
                    return None
                return _recv_exactly(sock, length)
        except OSError:
            return None


def _recv_exactly(sock: socket.socket, count: int) -> bytes | None:
    """Read exactly ``count`` bytes, or ``None`` if the peer stopped early."""
    chunks: list[bytes] = []
    remaining = count
    while remaining > 0:
        try:
            chunk = sock.recv(remaining)
        except OSError:
            return None
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)
