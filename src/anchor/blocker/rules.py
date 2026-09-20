"""The nftables table Anchor loads while a session runs (SPEC 8.2).

One table, ``inet anchor``, holding everything. Deleting it removes every rule
Anchor added, which is what makes restoration a single command that works even
when the journal is gone.

The table exists only during a session. That is what fails open (P4): with no
session there are no rules, so a blocker that dies while idle cannot take the
network with it. During a session the rules stay in the kernel even if the
daemon dies, so traffic stays blocked until systemd brings it back.

The first rule returns on Anchor's own mark. It has to be first: the resolver
forwards upstream on port 53, and without the exemption its own queries would
be redirected back into itself and nothing would resolve. Milestone 0 spike 2
proved both halves of that on a real machine.
"""

from __future__ import annotations

import ipaddress
import logging
from dataclasses import dataclass, field
from pathlib import Path

from anchor.blocker.commands import Runner, run
from anchor.blocker.constants import ANCHOR_MARK, NFT_FAMILY, NFT_TABLE, RESOLVER_PORT
from anchor.blocker.journal import Journal

log = logging.getLogger("anchor-blockerd")

#: DNS over TLS. Blocked during a session so a client cannot use it to reach a
#: resolver Anchor does not see.
DOT_PORT = 853


@dataclass(slots=True)
class FirewallPlan:
    """What the table should contain."""

    resolver_port: int = RESOLVER_PORT
    mark: int = ANCHOR_MARK
    doh_v4: list[str] = field(default_factory=list)
    doh_v6: list[str] = field(default_factory=list)
    blocked_v4: list[str] = field(default_factory=list)
    blocked_v6: list[str] = field(default_factory=list)
    """Addresses of names this session blocks, from the recent-answer map.

    Blocking DNS does nothing for a page already open: the address is known and
    the connection made. Rejecting these closes that door (SPEC 8.2).
    """

    block_dot: bool = True


def split_addresses(entries: list[str]) -> tuple[list[str], list[str]]:
    """Sort addresses and ranges into IPv4 and IPv6, dropping what is neither."""
    v4: list[str] = []
    v6: list[str] = []
    for entry in entries:
        try:
            network = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            log.warning("ignoring %r: not an address or range", entry)
            continue
        text = str(network) if network.num_addresses > 1 else str(network.network_address)
        (v4 if network.version == 4 else v6).append(text)
    return v4, v6


def build_ruleset(plan: FirewallPlan) -> str:
    """Render the nftables ruleset.

    Kept as text rather than built through a library so that what Anchor loads
    is exactly what a person can read in ``nft list table inet anchor``, and so
    the root daemons stay on the standard library (SPEC 5.4).
    """
    lines: list[str] = [f"table {NFT_FAMILY} {NFT_TABLE} {{"]

    if plan.doh_v4:
        lines += [
            "    set doh_v4 {",
            "        type ipv4_addr",
            "        flags interval",
            f"        elements = {{ {', '.join(plan.doh_v4)} }}",
            "    }",
        ]
    if plan.doh_v6:
        lines += [
            "    set doh_v6 {",
            "        type ipv6_addr",
            "        flags interval",
            f"        elements = {{ {', '.join(plan.doh_v6)} }}",
            "    }",
        ]
    if plan.blocked_v4:
        lines += [
            "    set blocked_v4 {",
            "        type ipv4_addr",
            "        flags interval",
            f"        elements = {{ {', '.join(plan.blocked_v4)} }}",
            "    }",
        ]
    if plan.blocked_v6:
        lines += [
            "    set blocked_v6 {",
            "        type ipv6_addr",
            "        flags interval",
            f"        elements = {{ {', '.join(plan.blocked_v6)} }}",
            "    }",
        ]

    # Redirect every DNS query to Anchor's resolver.
    lines += [
        "    chain redirect_dns {",
        "        type nat hook output priority -100; policy accept;",
        "",
        "        # Anchor's own resolver must reach the real world. This rule is",
        "        # first on purpose: without it the resolver's forwarded queries",
        "        # come straight back to it and nothing resolves.",
        f"        meta mark {plan.mark} return",
        "",
        "        # Everything else asking for DNS is answered by Anchor.",
        f"        meta l4proto {{ udp, tcp }} th dport 53 redirect to :{plan.resolver_port}",
        "    }",
    ]

    filter_rules: list[str] = []
    if plan.block_dot:
        filter_rules += [
            "        # DNS over TLS would reach a resolver Anchor cannot see.",
            f"        tcp dport {DOT_PORT} reject with tcp reset",
            f"        udp dport {DOT_PORT} drop",
        ]
    if plan.doh_v4:
        filter_rules += [
            "        # Known DNS-over-HTTPS endpoints, over HTTP/2 and HTTP/3.",
            "        ip daddr @doh_v4 tcp dport 443 reject with tcp reset",
            "        ip daddr @doh_v4 udp dport 443 drop",
        ]
    if plan.doh_v6:
        filter_rules += [
            "        ip6 daddr @doh_v6 tcp dport 443 reject with tcp reset",
            "        ip6 daddr @doh_v6 udp dport 443 drop",
        ]
    if plan.blocked_v4:
        filter_rules += [
            "        # Addresses of blocked names that were resolved before the",
            "        # session began. Rejecting rather than dropping so an open",
            "        # page fails at once instead of hanging.",
            "        ip daddr @blocked_v4 reject",
        ]
    if plan.blocked_v6:
        filter_rules += ["        ip6 daddr @blocked_v6 reject"]

    if filter_rules:
        lines += [
            "    chain block_encrypted_dns {",
            "        type filter hook output priority 0; policy accept;",
            "",
            f"        meta mark {plan.mark} return",
            "",
            *filter_rules,
            "    }",
        ]

    lines.append("}")
    return "\n".join(lines) + "\n"


def load_addresses(path: Path) -> list[str]:
    """Read an address list such as ``doh-endpoints.txt``."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        log.warning("could not read the address list at %s: %s", path, error)
        return []

    entries: list[str] = []
    for line in text.splitlines():
        entry = line.split("#", 1)[0].strip()
        if entry:
            entries.append(entry)
    return entries


def apply_rules(plan: FirewallPlan, journal: Journal, *, runner: Runner = run) -> None:
    """Load the table, replacing any previous one.

    The table is recorded before it is loaded. If the load half-succeeds, or
    the process dies between the two, restoration still knows to remove it;
    recording afterwards could leave rules nothing remembers.
    """
    journal.record_nft_table()

    ruleset = build_ruleset(plan)
    # Loading over an existing table merges rather than replaces, so the old
    # one goes first. Its absence is not an error.
    runner(["nft", "delete", "table", NFT_FAMILY, NFT_TABLE])

    result = runner(["nft", "-f", "-"], input_text=ruleset)
    if not result.ok:
        raise RuleLoadError(f"nftables refused the ruleset: {result.text}")

    log.info(
        "firewall rules loaded: DNS redirected to port %d, %d DoH endpoint(s) and "
        "%d already-resolved address(es) blocked",
        plan.resolver_port,
        len(plan.doh_v4) + len(plan.doh_v6),
        len(plan.blocked_v4) + len(plan.blocked_v6),
    )


def rules_loaded(*, runner: Runner = run) -> bool:
    """Whether Anchor's table is currently in the kernel."""
    return runner(["nft", "list", "table", NFT_FAMILY, NFT_TABLE]).ok


class RuleLoadError(RuntimeError):
    """nftables would not accept the ruleset."""
