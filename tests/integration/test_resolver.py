"""The resolver against a real socket and a real upstream (SPEC 8.2)."""

from __future__ import annotations

import logging
import socket
import struct
import threading
from collections.abc import Iterator

import pytest

from anchor.blocker.matcher import Policy
from anchor.blocker.resolver import Resolver, ResolverConfig
from anchor.protocol.types import WebMode

CANNED_ANSWER_MARKER = b"\xde\xad\xbe\xef"


def query(name: str, ident: int = 0x4242) -> bytes:
    labels = b"".join(bytes([len(p)]) + p.encode() for p in name.split(".") if p)
    return (
        struct.pack(">HHHHHH", ident, 0x0100, 1, 0, 0, 0)
        + labels
        + b"\x00"
        + struct.pack(">HH", 1, 1)
    )


def rcode(packet: bytes) -> int:
    flags: int = struct.unpack(">H", packet[2:4])[0]
    return flags & 0x000F


class FakeUpstream:
    """A DNS server that answers everything with the same recognisable reply."""

    def __init__(self) -> None:
        self.seen: list[bytes] = []
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.settimeout(0.3)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                packet, client = self._sock.recvfrom(4096)
            except TimeoutError:
                continue
            except OSError:
                break
            self.seen.append(packet)
            # A response: same id, QR set, no error, plus a marker.
            ident = packet[:2]
            reply = ident + struct.pack(">HHHHH", 0x8180, 1, 1, 0, 0)
            self._sock.sendto(reply + packet[12:] + CANNED_ANSWER_MARKER, client)

    def __enter__(self) -> FakeUpstream:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        self._sock.close()


@pytest.fixture
def upstream() -> Iterator[FakeUpstream]:
    with FakeUpstream() as server:
        yield server


class Harness:
    """A resolver with a policy that tests can swap."""

    def __init__(self, upstream_port: int) -> None:
        self.policy: Policy | None = None
        self.blocked: list[tuple[str, str]] = []
        self.config = ResolverConfig(
            upstreams=["127.0.0.1"],
            port=0,
            upstream_port=upstream_port,
            mark=0,
        )
        self.resolver = Resolver(
            self.config,
            policy=lambda: self.policy,
            on_blocked=lambda name, rule: self.blocked.append((name, rule)),
        )

    def ask(self, name: str) -> bytes:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(5)
        try:
            sock.sendto(query(name), ("127.0.0.1", self.resolver.port))
            return sock.recv(4096)
        finally:
            sock.close()


@pytest.fixture
def harness(upstream: FakeUpstream) -> Iterator[Harness]:
    harness = Harness(upstream.port)
    harness.resolver.start()
    try:
        yield harness
    finally:
        harness.resolver.stop()


class TestWithNoSession:
    def test_everything_is_forwarded(self, harness: Harness, upstream: FakeUpstream) -> None:
        reply = harness.ask("example.com")

        assert CANNED_ANSWER_MARKER in reply
        assert len(upstream.seen) == 1

    def test_the_reply_comes_back_untouched(self, harness: Harness) -> None:
        """The resolver rewrites nothing (SPEC 8.2)."""
        reply = harness.ask("example.com")
        assert reply.endswith(CANNED_ANSWER_MARKER)


class TestWithABlocklist:
    def test_a_blocked_name_gets_nxdomain(self, harness: Harness, upstream: FakeUpstream) -> None:
        harness.policy = Policy(WebMode.BLOCKLIST, frozenset({"youtube.com"}))

        reply = harness.ask("www.youtube.com")

        assert rcode(reply) == 3
        assert not upstream.seen, "a blocked name was still sent upstream"

    def test_an_allowed_name_is_forwarded(self, harness: Harness, upstream: FakeUpstream) -> None:
        harness.policy = Policy(WebMode.BLOCKLIST, frozenset({"youtube.com"}))

        reply = harness.ask("example.com")

        assert CANNED_ANSWER_MARKER in reply
        assert len(upstream.seen) == 1

    def test_the_identifier_matches_so_the_client_accepts_it(self, harness: Harness) -> None:
        harness.policy = Policy(WebMode.BLOCKLIST, frozenset({"youtube.com"}))

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(5)
        sock.sendto(query("youtube.com", ident=0x7A7A), ("127.0.0.1", harness.resolver.port))
        reply = sock.recv(4096)
        sock.close()

        assert struct.unpack(">H", reply[:2])[0] == 0x7A7A


class TestWithAnAllowlist:
    def test_an_unlisted_name_is_blocked(self, harness: Harness, upstream: FakeUpstream) -> None:
        harness.policy = Policy(WebMode.ALLOWLIST, frozenset({"wikipedia.org"}))

        assert rcode(harness.ask("youtube.com")) == 3
        assert not upstream.seen

    def test_a_listed_name_is_forwarded(self, harness: Harness) -> None:
        harness.policy = Policy(WebMode.ALLOWLIST, frozenset({"wikipedia.org"}))
        assert CANNED_ANSWER_MARKER in harness.ask("en.wikipedia.org")

    def test_essentials_survive(self, harness: Harness) -> None:
        harness.policy = Policy(
            WebMode.ALLOWLIST,
            frozenset({"wikipedia.org"}),
            essentials=frozenset({"ntp.ubuntu.com"}),
        )
        assert CANNED_ANSWER_MARKER in harness.ask("ntp.ubuntu.com")


class TestReporting:
    def test_a_blocked_attempt_is_reported_with_its_rule(self, harness: Harness) -> None:
        harness.policy = Policy(WebMode.BLOCKLIST, frozenset({"youtube.com"}))

        harness.ask("m.youtube.com")

        assert harness.blocked == [("m.youtube.com", "youtube.com")]

    def test_allowed_names_are_not_reported(self, harness: Harness) -> None:
        harness.policy = Policy(WebMode.BLOCKLIST, frozenset({"youtube.com"}))
        harness.ask("example.com")
        assert harness.blocked == []

    def test_a_handler_that_raises_does_not_break_resolution(self, harness: Harness) -> None:
        """Losing the statistics database must not take name resolution with it."""

        def explode(name: str, rule: str) -> None:
            raise RuntimeError("the statistics database is on fire")

        harness.policy = Policy(WebMode.BLOCKLIST, frozenset({"youtube.com"}))
        harness.resolver._on_blocked = explode  # noqa: SLF001

        assert rcode(harness.ask("youtube.com")) == 3

    def test_no_domain_reaches_the_log(
        self, harness: Harness, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The working rules forbid logging visited domains (SPEC 13)."""
        harness.policy = Policy(WebMode.BLOCKLIST, frozenset({"secret-site.example"}))

        with caplog.at_level(logging.DEBUG, logger="anchor-blockerd"):
            harness.ask("secret-site.example")
            harness.ask("another-private-site.example")

        assert "secret-site" not in caplog.text
        assert "another-private-site" not in caplog.text


class TestRubbish:
    def test_a_malformed_query_is_ignored_rather_than_answered(self, harness: Harness) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(1.0)
        sock.sendto(b"\x00\x01\x02", ("127.0.0.1", harness.resolver.port))

        with pytest.raises(TimeoutError):
            sock.recv(4096)
        sock.close()

        # And the resolver is still working.
        assert CANNED_ANSWER_MARKER in harness.ask("example.com")

    def test_an_empty_packet_does_not_crash_it(self, harness: Harness) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.sendto(b"", ("127.0.0.1", harness.resolver.port))
        sock.close()

        assert CANNED_ANSWER_MARKER in harness.ask("example.com")


class TestWhenUpstreamIsGone:
    def test_no_upstream_means_servfail_not_nxdomain(self, harness: Harness) -> None:
        """Claiming a name does not exist would be a lie clients cache."""
        harness.resolver.set_upstreams([])

        assert rcode(harness.ask("example.com")) == 2
