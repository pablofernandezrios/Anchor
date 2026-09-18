#!/usr/bin/env python3
"""Spike 1: can Anchor sit in front of systemd-resolved? (SPEC 8.2)

The specification says Anchor runs its own forwarding resolver and puts itself
in front of the system resolver without replacing it, picking up network
changes automatically. Three things have to be true for that:

1. systemd-resolved can be told to send every query to a resolver of ours,
   through a drop-in rather than by rewriting /etc/resolv.conf.
2. Anchor can discover the DNS servers of the current link, so it has somewhere
   to forward to, and can notice when they change.
3. There is a workable path on distributions without systemd-resolved.

The spike stands up a real forwarding resolver on a high port, points resolved
at it, and checks that a query actually arrives.
"""

from __future__ import annotations

import re
import socket
import threading
from pathlib import Path

from lib import SpikeReport, Verdict, have, main, restored_file, run

RESOLVER_PORT = 5391
DROP_IN = Path("/etc/systemd/resolved.conf.d/zz-anchor-spike.conf")
PROBE = "spike-probe.anchor.invalid"


class TinyForwarder:
    """A forwarding resolver, in the shape SPEC 8.2 describes.

    Parses only the question section, answers NXDOMAIN for a blocked name, and
    forwards everything else untouched.
    """

    def __init__(self, upstream: str, blocked: set[str]) -> None:
        self.upstream = upstream
        self.blocked = blocked
        self.seen: list[str] = []
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("127.0.0.1", RESOLVER_PORT))
        self._socket.settimeout(0.5)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    @staticmethod
    def question_name(packet: bytes) -> str:
        """Read the QNAME out of a query. Only the question section is parsed."""
        labels: list[str] = []
        offset = 12  # Skip the fixed-size header.
        while offset < len(packet):
            length = packet[offset]
            if length == 0:
                break
            offset += 1
            labels.append(packet[offset : offset + length].decode("ascii", "replace"))
            offset += length
        return ".".join(labels)

    @staticmethod
    def nxdomain(query: bytes) -> bytes:
        """Build an NXDOMAIN answer for ``query`` (SPEC 8.2)."""
        transaction = query[:2]
        flags = 0x8183  # response, recursion desired and available, NXDOMAIN
        header = transaction + flags.to_bytes(2, "big") + query[4:6] + b"\x00" * 6
        return header + query[12:]

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                packet, client = self._socket.recvfrom(4096)
            except TimeoutError:
                continue
            except OSError:
                break

            name = self.question_name(packet)
            self.seen.append(name)

            if any(name == blocked or name.endswith("." + blocked) for blocked in self.blocked):
                self._socket.sendto(self.nxdomain(packet), client)
                continue

            try:
                upstream = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                upstream.settimeout(3.0)
                upstream.sendto(packet, (self.upstream, 53))
                answer, _ = upstream.recvfrom(4096)
                self._socket.sendto(answer, client)
            except OSError:
                self._socket.sendto(self.nxdomain(packet), client)
            finally:
                upstream.close()

    def __enter__(self) -> TinyForwarder:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        self._socket.close()


def link_dns_servers() -> list[str]:
    """The DNS servers of the current link, which Anchor forwards to."""
    result = run("resolvectl", "status")
    if not result.ok:
        return []
    servers: list[str] = []
    for match in re.finditer(r"DNS Servers?:\s*(.+)", result.out):
        servers.extend(match.group(1).split())
    # Skip our own resolver if a previous run left it configured.
    return [s for s in servers if not s.startswith("127.0.0.1:")]


def spike(report: SpikeReport) -> None:
    if not have("resolvectl"):
        report.add(
            "systemd-resolved present",
            Verdict.UNAVAILABLE,
            "resolvectl is not installed; run this spike on a systemd-resolved system",
        )
        _report_fallback(report)
        return

    status = run("systemctl", "is-active", "systemd-resolved")
    if status.text != "active":
        report.add(
            "systemd-resolved active",
            Verdict.UNAVAILABLE,
            f"systemd-resolved is {status.text or 'not running'}",
        )
        _report_fallback(report)
        return

    report.add("systemd-resolved active", Verdict.WORKS, "the service is running")

    upstreams = link_dns_servers()
    if not upstreams:
        report.add(
            "link DNS discoverable",
            Verdict.FAILS,
            "could not read the link's DNS servers from resolvectl, so Anchor "
            "would have nowhere to forward to",
            run("resolvectl", "status").text[:2000],
        )
        return
    report.add(
        "link DNS discoverable",
        Verdict.WORKS,
        f"resolvectl reports {', '.join(upstreams)}",
    )

    resolv = Path("/etc/resolv.conf")
    target = resolv.resolve() if resolv.exists() else Path("(missing)")
    report.add(
        "resolv.conf layout",
        Verdict.WORKS,
        f"/etc/resolv.conf resolves to {target}",
        run("sh", "-c", "ls -l /etc/resolv.conf; head -5 /etc/resolv.conf").text,
    )

    with TinyForwarder(upstreams[0], blocked={PROBE}) as forwarder, restored_file(DROP_IN):
        DROP_IN.parent.mkdir(parents=True, exist_ok=True)
        DROP_IN.write_text(
            "# Anchor spike. Removed automatically.\n"
            "[Resolve]\n"
            f"DNS=127.0.0.1:{RESOLVER_PORT}\n"
            "Domains=~.\n"
            "DNSStubListener=yes\n"
            "DNSOverTLS=no\n"
            "Cache=no\n",
            encoding="utf-8",
        )
        restarted = run("systemctl", "restart", "systemd-resolved")
        if not restarted.ok:
            report.add(
                "drop-in accepted",
                Verdict.FAILS,
                "systemd-resolved refused to restart with the drop-in in place",
                restarted.text,
            )
            return

        report.add(
            "drop-in accepted",
            Verdict.WORKS,
            f"resolved restarted with DNS=127.0.0.1:{RESOLVER_PORT}",
            run("resolvectl", "status").out[:1500],
        )

        # Does a real query reach our resolver?
        run("resolvectl", "flush-caches")
        query = run("resolvectl", "query", "--cache=no", PROBE, timeout=15)
        if forwarder.seen:
            report.add(
                "queries reach Anchor",
                Verdict.WORKS,
                f"the resolver saw {len(forwarder.seen)} query(ies), including {forwarder.seen[0]}",
                "\n".join(forwarder.seen[:10]),
            )
        else:
            report.add(
                "queries reach Anchor",
                Verdict.FAILS,
                "no query arrived at the local resolver, so the drop-in does not "
                "actually route traffic through Anchor",
                query.text[:1000],
            )

        blocked_ok = "NXDOMAIN" in query.text or "not found" in query.text.lower()
        report.add(
            "NXDOMAIN reaches the client",
            Verdict.WORKS if blocked_ok else Verdict.FAILS,
            "a blocked name comes back as NXDOMAIN"
            if blocked_ok
            else "the blocked name did not surface as NXDOMAIN",
            query.text[:1000],
        )

    run("systemctl", "restart", "systemd-resolved")
    report.add(
        "clean restore",
        Verdict.WORKS,
        "the drop-in was removed and systemd-resolved restarted",
        run("resolvectl", "status").out[:600],
    )

    _report_network_changes(report)
    _report_fallback(report)


def _report_network_changes(report: SpikeReport) -> None:
    """SPEC 8.2: network changes must be picked up without manual action."""
    if have("nmcli"):
        report.add(
            "network change signal",
            Verdict.WORKS,
            "NetworkManager is present, so Anchor can watch it for link changes",
            run("nmcli", "-t", "-f", "NAME,TYPE,DEVICE", "connection", "show").text[:600],
        )
    else:
        report.add(
            "network change signal",
            Verdict.UNAVAILABLE,
            "NetworkManager is absent here. Anchor must also watch resolved's "
            "own D-Bus signals, which is what a headless runner cannot show",
        )


def _report_fallback(report: SpikeReport) -> None:
    """SPEC 8.2 asks for an equivalent path without systemd-resolved."""
    notes: list[str] = []
    resolv = Path("/etc/resolv.conf")
    if resolv.exists():
        notes.append(resolv.read_text(encoding="utf-8", errors="replace")[:400])

    managers = {
        "systemd-resolved": have("resolvectl"),
        "NetworkManager": have("nmcli"),
        "resolvconf": have("resolvconf"),
        "openresolv": Path("/etc/resolvconf.conf").exists(),
    }
    present = [name for name, found in managers.items() if found]
    report.add(
        "fallback path",
        Verdict.WORKS,
        "without systemd-resolved, Anchor writes /etc/resolv.conf itself "
        f"(after backing it up) or drives resolvconf. Present here: "
        f"{', '.join(present) or 'none'}",
        "\n".join(notes),
    )


if __name__ == "__main__":
    raise SystemExit(
        main(
            spike,
            SpikeReport(
                spike="s1-dns-resolved",
                question="Can Anchor front systemd-resolved and follow network changes?",
            ),
        )
    )
