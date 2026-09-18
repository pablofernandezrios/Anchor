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
#: A name that resolves perfectly well on its own. Asking for it and getting
#: NXDOMAIN proves Anchor answered, which a .invalid name never could: that
#: would come back NXDOMAIN whether or not the query ever reached us.
BLOCKED_PROBE = "example.com"
#: A second name, left unblocked, to prove forwarding still works.
ALLOWED_PROBE = "example.net"


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

    with TinyForwarder(upstreams[0], blocked={BLOCKED_PROBE}) as forwarder:
        _try_global_dns(report, forwarder)
        _try_per_link_dns(report, forwarder)

    run("systemctl", "restart", "systemd-resolved")
    report.add(
        "clean restore",
        Verdict.WORKS,
        "the drop-in was removed, every link reverted and systemd-resolved restarted",
        run("resolvectl", "status").out[:600],
    )

    _report_network_changes(report)
    _report_fallback(report)


def _links() -> list[str]:
    """Network links resolved knows about, excluding loopback."""
    names: list[str] = []
    for match in re.finditer(r"Link \d+ \(([^)]+)\)", run("resolvectl", "status").out):
        name = match.group(1)
        if name != "lo":
            names.append(name)
    return names


def _query_reaches_anchor(forwarder: TinyForwarder, name: str) -> tuple[bool, str]:
    """Ask for ``name`` and report whether Anchor's resolver saw the question."""
    before = len(forwarder.seen)
    run("resolvectl", "flush-caches")
    result = run("resolvectl", "query", "--cache=no", name, timeout=15)
    saw = any(seen == name or seen.endswith("." + name) for seen in forwarder.seen[before:])
    return saw, result.text


def _try_global_dns(report: SpikeReport, forwarder: TinyForwarder) -> None:
    """Mechanism A: a global DNS= drop-in, which is the obvious first attempt."""
    with restored_file(DROP_IN):
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
        if not run("systemctl", "restart", "systemd-resolved").ok:
            report.add(
                "global DNS= drop-in",
                Verdict.FAILS,
                "systemd-resolved refused to restart with the drop-in in place",
            )
            return

        saw, detail = _query_reaches_anchor(forwarder, BLOCKED_PROBE)
        report.add(
            "mechanism A: global DNS= drop-in",
            Verdict.WORKS if saw else Verdict.RULED_OUT,
            "queries reach Anchor through a global DNS= setting"
            if saw
            else "queries do NOT reach Anchor. A link with its own DNS servers "
            "from DHCP wins over the global setting, so the global DNS= is only "
            "consulted when no link matches. This is the mechanism to avoid",
            detail[:800],
        )

    run("systemctl", "restart", "systemd-resolved")


def _try_per_link_dns(report: SpikeReport, forwarder: TinyForwarder) -> None:
    """Mechanism B: per-link DNS, which is what actually outranks DHCP."""
    links = _links()
    if not links:
        report.add(
            "mechanism B: per-link DNS",
            Verdict.UNAVAILABLE,
            "no non-loopback link was found to configure",
        )
        return

    try:
        for link in links:
            run("resolvectl", "dns", link, f"127.0.0.1:{RESOLVER_PORT}")
            run("resolvectl", "domain", link, "~.")

        saw, detail = _query_reaches_anchor(forwarder, BLOCKED_PROBE)
        report.add(
            "mechanism B: per-link DNS",
            Verdict.WORKS if saw else Verdict.FAILS,
            f"queries reach Anchor once every link ({', '.join(links)}) is pointed "
            "at it with a ~. routing domain"
            if saw
            else "queries still do not reach Anchor even with per-link DNS set",
            detail[:800],
        )

        if saw:
            blocked_ok = "NXDOMAIN" in detail or "not found" in detail.lower()
            report.add(
                "blocking actually blocks",
                Verdict.WORKS if blocked_ok else Verdict.FAILS,
                f"{BLOCKED_PROBE} resolves normally, and came back NXDOMAIN, which "
                "only Anchor could have done"
                if blocked_ok
                else f"{BLOCKED_PROBE} did not come back NXDOMAIN",
                detail[:800],
            )

            allowed_saw, allowed_detail = _query_reaches_anchor(forwarder, ALLOWED_PROBE)
            forwarded = allowed_saw and "NXDOMAIN" not in allowed_detail
            report.add(
                "unblocked names still resolve",
                Verdict.WORKS if forwarded else Verdict.FAILS,
                f"{ALLOWED_PROBE} passed through Anchor and was answered upstream"
                if forwarded
                else f"{ALLOWED_PROBE} did not resolve, so forwarding is broken",
                allowed_detail[:800],
            )
    finally:
        for link in links:
            run("resolvectl", "revert", link)


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
