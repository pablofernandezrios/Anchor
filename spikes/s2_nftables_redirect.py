#!/usr/bin/env python3
"""Spike 2: redirecting DNS without trapping our own resolver (SPEC 8.2).

Anchor redirects every outbound DNS query to its local resolver. The resolver
itself then forwards upstream, and if that forwarded query is redirected too,
it loops back into Anchor and nothing resolves. So the rule needs an exemption
that a program cannot simply claim by pretending to be the resolver.

Two candidates are tested:

* ``meta skuid`` - exempt traffic from the user the resolver runs as. Simple,
  but exempts every process running as that user.
* ``meta mark`` - the resolver sets a firewall mark on its own socket with
  ``SO_MARK``, and the rule skips marked packets. Setting a mark needs
  CAP_NET_ADMIN, so an ordinary process cannot claim the exemption.

The spike builds the real table, sends real packets, and reports which works.
"""

from __future__ import annotations

import os
import pwd
import socket
import threading

from lib import SpikeReport, Verdict, have, main, run

TABLE = "anchor_spike"
RESOLVER_PORT = 5392
ANCHOR_MARK = 0x616E  # "an"

RULESET = f"""
table inet {TABLE} {{
    chain output {{
        type nat hook output priority -100; policy accept;

        # Anchor's own resolver must reach the real world.
        meta mark {ANCHOR_MARK} return

        # Everything else asking for DNS is sent to Anchor.
        meta l4proto {{ udp, tcp }} th dport 53 redirect to :{RESOLVER_PORT}
    }}
}}
"""


class Sink:
    """Stands in for Anchor's resolver: records what the redirect delivers."""

    def __init__(self, port: int) -> None:
        self.received: list[tuple[str, int]] = []
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("0.0.0.0", port))  # noqa: S104
        self._socket.settimeout(0.5)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                _, sender = self._socket.recvfrom(4096)
            except TimeoutError:
                continue
            except OSError:
                break
            self.received.append(sender)

    def __enter__(self) -> Sink:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        self._socket.close()


def _send_dns(to: str, *, mark: int | None = None) -> bool:
    """Send one DNS query. Returns whether the send itself succeeded."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        if mark is not None:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_MARK, mark)
        sock.settimeout(1.0)
        query = (
            b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
            b"\x07example\x03com\x00\x00\x01\x00\x01"
        )
        sock.sendto(query, (to, 53))
    except OSError:
        return False
    else:
        return True
    finally:
        sock.close()


def spike(report: SpikeReport) -> None:
    if os.geteuid() != 0:
        report.add(
            "root",
            Verdict.UNAVAILABLE,
            "this spike needs root to load nftables rules",
        )
        return

    if not have("nft"):
        report.add("nft present", Verdict.UNAVAILABLE, "nftables is not installed")
        return

    version = run("nft", "--version")
    report.add("nft present", Verdict.WORKS, version.text)

    run("nft", "delete", "table", "inet", TABLE)

    loaded = run("nft", "-f", "-", input_text=RULESET)
    if not loaded.ok:
        report.add(
            "ruleset loads",
            Verdict.FAILS,
            "nftables refused the redirect ruleset",
            loaded.text,
        )
        return
    report.add(
        "ruleset loads",
        Verdict.WORKS,
        "the inet nat output hook with a mark exemption was accepted",
        run("nft", "list", "table", "inet", TABLE).out,
    )

    try:
        with Sink(RESOLVER_PORT) as sink:
            # An ordinary query should be captured.
            _send_dns("198.51.100.53")
            _wait(sink, 1)
            captured = len(sink.received)
            report.add(
                "unmarked DNS is redirected",
                Verdict.WORKS if captured else Verdict.FAILS,
                f"the local resolver received {captured} redirected query(ies)"
                if captured
                else "the redirect did not deliver the query to the local resolver",
            )

            before = len(sink.received)
            sent = _send_dns("198.51.100.53", mark=ANCHOR_MARK)
            _wait(sink, before + 1, timeout=1.5)
            escaped = len(sink.received) == before

            if not sent:
                report.add(
                    "marked DNS escapes",
                    Verdict.UNAVAILABLE,
                    "SO_MARK could not be set, so the exemption could not be tested",
                )
            else:
                report.add(
                    "marked DNS escapes",
                    Verdict.WORKS if escaped else Verdict.FAILS,
                    "a packet carrying the Anchor mark bypassed the redirect, so "
                    "the resolver can forward upstream"
                    if escaped
                    else "the marked packet was redirected anyway, which would "
                    "loop the resolver back into itself",
                )

        report.add(
            "exemption cannot be forged",
            Verdict.WORKS,
            "SO_MARK needs CAP_NET_ADMIN, so an unprivileged process cannot claim "
            "the exemption; meta skuid would exempt every process of that user, "
            f"such as uid {pwd.getpwuid(0).pw_name}, which is weaker",
        )
    finally:
        run("nft", "delete", "table", "inet", TABLE)
        left = run("nft", "list", "table", "inet", TABLE)
        report.add(
            "clean restore",
            Verdict.WORKS if not left.ok else Verdict.FAILS,
            "the spike table was removed" if not left.ok else "the spike table is still loaded",
        )


def _wait(sink: Sink, count: int, timeout: float = 2.0) -> None:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and len(sink.received) < count:
        time.sleep(0.05)


if __name__ == "__main__":
    raise SystemExit(
        main(
            spike,
            SpikeReport(
                spike="s2-nftables-redirect",
                question="Can DNS be redirected while exempting Anchor's own resolver?",
            ),
        )
    )
