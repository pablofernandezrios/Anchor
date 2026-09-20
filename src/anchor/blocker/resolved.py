"""Pointing systemd-resolved at Anchor, and following the network (SPEC 8.2).

Worth being clear about what this is for, because the end-to-end test changed
the answer. The nftables redirect is the universal mechanism: it catches every
query to port 53 from every process, whatever ``/etc/resolv.conf`` says and
whether or not systemd-resolved exists. Anchor blocks fine on a machine with
no resolved at all, which is the equivalent path SPEC 8.2 asks for on
distributions without it.

Configuring resolved on top of that buys two things:

* **One hop instead of two.** Left alone, resolved forwards to its own upstream
  and that query is redirected back to Anchor, so every lookup crosses the
  resolver twice.
* **No cache in front of the block.** ``Cache=no`` matters more than it looks.
  A domain blocked in the middle of a session would otherwise keep resolving
  from resolved's cache until the entry expired, and the user would reasonably
  conclude that Anchor does not work.

Everything written here goes under ``/run``, never ``/etc``. A drop-in in
``/etc`` pointing DNS at a resolver that is not running would leave a machine
unable to resolve anything until somebody found and deleted the file; under
``/run`` the worst case is one reboot (P4).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from anchor.blocker.commands import Runner, run
from anchor.blocker.constants import (
    RESOLVED_DROP_IN,
    RESOLVER_ADDRESS,
    RESOLVER_PORT,
)
from anchor.blocker.journal import Journal

log = logging.getLogger("anchor-blockerd")

_LINK = re.compile(r"Link \d+ \(([^)]+)\)")
_DNS_SERVERS = re.compile(r"DNS Servers?:\s*(.+)")

#: Links that never carry the traffic Anchor cares about.
_SKIP_LINKS = frozenset({"lo"})


def _drop_in_body(port: int) -> str:
    return (
        "# Written by Anchor while a session is active (SPEC 8.2).\n"
        "# Lives under /run, so a reboot clears it and a machine can never be\n"
        "# left unable to resolve names because Anchor is not running.\n"
        "[Resolve]\n"
        f"DNS={RESOLVER_ADDRESS}:{port}\n"
        "Domains=~.\n"
        "DNSOverTLS=no\n"
        "# Without this, a domain blocked mid-session keeps resolving from the\n"
        "# cache until its entry expires.\n"
        "Cache=no\n"
    )


@dataclass(slots=True)
class NetworkState:
    """What resolved currently reports, used to notice a change."""

    links: list[str] = field(default_factory=list)
    upstreams: list[str] = field(default_factory=list)

    def fingerprint(self) -> str:
        return "|".join(sorted(self.links)) + "//" + "|".join(sorted(self.upstreams))


def is_available(*, runner: Runner = run) -> bool:
    """Whether this machine runs systemd-resolved."""
    if not runner(["resolvectl", "--version"]).ok:
        return False
    return runner(["systemctl", "is-active", "systemd-resolved"]).text == "active"


def read_state(*, runner: Runner = run, resolver_port: int = RESOLVER_PORT) -> NetworkState:
    """Read the links and their DNS servers.

    Anchor's own address is filtered out of the upstreams. Without that,
    re-reading the state after configuring resolved would point the resolver at
    itself, and every query would spin between the two.
    """
    result = runner(["resolvectl", "status"])
    if not result.ok:
        return NetworkState()

    links = [name for name in _LINK.findall(result.out) if name not in _SKIP_LINKS]

    ours = {f"{RESOLVER_ADDRESS}:{resolver_port}", RESOLVER_ADDRESS}
    upstreams: list[str] = []
    for match in _DNS_SERVERS.finditer(result.out):
        for server in match.group(1).split():
            if server not in ours and server not in upstreams:
                upstreams.append(server)

    return NetworkState(links=links, upstreams=upstreams)


def apply(
    journal: Journal,
    *,
    runner: Runner = run,
    resolver_port: int = RESOLVER_PORT,
    drop_in: Path = RESOLVED_DROP_IN,
) -> NetworkState:
    """Point resolved at Anchor. Returns the upstreams to forward to.

    The upstreams are read first, before anything is changed. Reading them
    afterwards would find Anchor's own address and nothing else.
    """
    state = read_state(runner=runner, resolver_port=resolver_port)
    if not state.upstreams:
        log.warning(
            "resolved reports no upstream DNS servers; Anchor has nowhere to "
            "forward to and will answer SERVFAIL until the network comes back"
        )

    journal.write_file(drop_in, _drop_in_body(resolver_port))

    for link in state.links:
        # Per-link configuration outranks whatever DHCP supplied. The global
        # drop-in above is the backstop for a link Anchor did not enumerate.
        dns = runner(["resolvectl", "dns", link, f"{RESOLVER_ADDRESS}:{resolver_port}"])
        domains = runner(["resolvectl", "domain", link, "~."])
        if dns.ok and domains.ok:
            journal.record_link(link)
        else:
            log.warning(
                "could not point %s at Anchor (%s); the firewall redirect still "
                "catches its queries",
                link,
                (dns.text or domains.text)[:200],
            )

    if not runner(["systemctl", "restart", "systemd-resolved"]).ok:
        log.warning("systemd-resolved would not restart; the drop-in may not be in effect")

    runner(["resolvectl", "flush-caches"])
    log.info("resolved pointed at Anchor on %d links", len(state.links))
    return state


def follow_network_changes(
    journal: Journal,
    previous: NetworkState,
    *,
    runner: Runner = run,
    resolver_port: int = RESOLVER_PORT,
    drop_in: Path = RESOLVED_DROP_IN,
) -> NetworkState | None:
    """Re-apply if the network moved. Returns the new state, or ``None``.

    SPEC 3 says a laptop that changes networks often has to work without manual
    action, and SPEC 8.2 says network changes must be picked up automatically.
    Anchor compares what resolved reports against what it saw last time, which
    needs no D-Bus client in a daemon restricted to the standard library
    (SPEC 5.4), and catches a new link, a lost link and a changed server alike.
    """
    current = read_state(runner=runner, resolver_port=resolver_port)
    if current.fingerprint() == previous.fingerprint():
        return None

    log.info(
        "the network changed (%d link(s), %d upstream(s)); re-applying",
        len(current.links),
        len(current.upstreams),
    )
    return apply(journal, runner=runner, resolver_port=resolver_port, drop_in=drop_in)


#: Where a machine without systemd-resolved says its DNS servers are.
RESOLV_CONF: Final = Path("/etc/resolv.conf")

#: systemd-resolved's own stub. Forwarding to it while resolved is not running
#: sends queries nowhere; forwarding to it while resolved *is* running sends
#: them back through the redirect and into Anchor again.
_STUB_ADDRESSES: Final = frozenset({"127.0.0.53", "127.0.0.54"})


def read_resolv_conf(path: Path | None = None, *, resolver_port: int = RESOLVER_PORT) -> list[str]:
    """Read upstream servers from ``resolv.conf`` (SPEC 8.2).

    This is the path for distributions without systemd-resolved, and it is not
    a nicety. Without it the resolver has nowhere to forward, answers SERVFAIL
    to everything, and a session blocks the entire internet rather than the
    sites it was asked to block.
    """
    del resolver_port  # Anchor never appears in resolv.conf; it uses a port.

    # Resolved at call time rather than bound as a default, so the path is
    # one thing a test can move.
    path = path if path is not None else RESOLV_CONF

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        log.warning("could not read %s: %s", path, error)
        return []

    servers: list[str] = []
    for line in text.splitlines():
        entry = line.split("#", 1)[0].split(";", 1)[0].strip()
        if not entry.lower().startswith("nameserver"):
            continue
        parts = entry.split()
        if len(parts) < 2:
            continue
        address = parts[1]
        # Skip anything that would send the query straight back to Anchor.
        if address in _STUB_ADDRESSES or address.startswith("127.") or address == "::1":
            continue
        if address not in servers:
            servers.append(address)

    return servers


def discover_upstreams(*, runner: Runner = run, resolver_port: int = RESOLVER_PORT) -> list[str]:
    """Find somewhere to forward to, however this machine is configured."""
    if is_available(runner=runner):
        upstreams = read_state(runner=runner, resolver_port=resolver_port).upstreams
        if upstreams:
            return upstreams

    return read_resolv_conf(resolver_port=resolver_port)
