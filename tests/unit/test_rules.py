"""The nftables ruleset Anchor loads (SPEC 8.2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from anchor.blocker.journal import Journal
from anchor.blocker.rules import (
    FirewallPlan,
    RuleLoadError,
    apply_rules,
    build_ruleset,
    load_addresses,
    split_addresses,
)
from anchor.system.commands import RecordingRunner, Result


@pytest.fixture
def journal(tmp_path: Path) -> Journal:
    return Journal(tmp_path / "applied.json")


class TestTheExemptionComesFirst:
    def test_the_mark_rule_precedes_the_redirect(self) -> None:
        """Otherwise the resolver's own queries loop back into it."""
        ruleset = build_ruleset(FirewallPlan(mark=0x616E, resolver_port=5391))

        mark_at = ruleset.index("meta mark 24942 return")
        redirect_at = ruleset.index("redirect to :5391")
        assert mark_at < redirect_at

    def test_the_redirect_covers_udp_and_tcp(self) -> None:
        ruleset = build_ruleset(FirewallPlan())
        assert "meta l4proto { udp, tcp } th dport 53" in ruleset


class TestEncryptedDns:
    def test_dns_over_tls_is_blocked(self) -> None:
        ruleset = build_ruleset(FirewallPlan())
        assert "tcp dport 853 reject with tcp reset" in ruleset
        assert "udp dport 853 drop" in ruleset

    def test_doh_endpoints_are_blocked_over_both_protocols(self) -> None:
        """A browser falling back to HTTP/3 must not slip past."""
        ruleset = build_ruleset(FirewallPlan(doh_v4=["1.1.1.1"]))

        assert "ip daddr @doh_v4 tcp dport 443 reject with tcp reset" in ruleset
        assert "ip daddr @doh_v4 udp dport 443 drop" in ruleset

    def test_an_empty_set_is_not_emitted(self) -> None:
        """nftables will not accept a set declaration with no elements."""
        ruleset = build_ruleset(FirewallPlan(doh_v4=[], doh_v6=[]))

        assert "doh_v4" not in ruleset
        assert "doh_v6" not in ruleset

    def test_the_filter_chain_is_dropped_when_there_is_nothing_to_filter(self) -> None:
        ruleset = build_ruleset(FirewallPlan(block_dot=False))
        assert "block_encrypted_dns" not in ruleset

    def test_the_resolver_is_exempt_from_the_filter_chain_too(self) -> None:
        ruleset = build_ruleset(FirewallPlan(doh_v4=["1.1.1.1"]))
        chain = ruleset[ruleset.index("chain block_encrypted_dns") :]
        assert chain.index("meta mark") < chain.index("tcp dport 853")


class TestSortingAddresses:
    def test_addresses_are_split_by_family(self) -> None:
        v4, v6 = split_addresses(["1.1.1.1", "2606:4700:4700::1111", "8.8.8.8"])

        assert v4 == ["1.1.1.1", "8.8.8.8"]
        assert v6 == ["2606:4700:4700::1111"]

    def test_ranges_are_kept_as_ranges(self) -> None:
        v4, _ = split_addresses(["45.90.28.0/24"])
        assert v4 == ["45.90.28.0/24"]

    def test_rubbish_is_dropped_rather_than_fatal(self) -> None:
        v4, v6 = split_addresses(["1.1.1.1", "not-an-address", "example.com"])

        assert v4 == ["1.1.1.1"]
        assert v6 == []


class TestTheShippedLists:
    def test_the_doh_endpoint_list_is_all_addresses(self) -> None:
        shipped = Path(__file__).resolve().parents[2] / "data" / "doh-endpoints.txt"
        entries = load_addresses(shipped)
        v4, v6 = split_addresses(entries)

        assert len(entries) >= 20
        assert len(v4) + len(v6) == len(entries), "an entry is not a valid address"
        assert "1.1.1.1" in v4
        assert "45.90.28.0/24" in v4

    def test_the_doh_domain_list_is_all_domains(self) -> None:
        from anchor.blocker.matcher import load_domain_file

        shipped = Path(__file__).resolve().parents[2] / "data" / "doh-domains.txt"
        domains = load_domain_file(shipped)

        assert "dns.google" in domains
        assert "mozilla.cloudflare-dns.com" in domains
        assert len(domains) >= 20


class TestApplying:
    def test_the_table_is_recorded_before_it_is_loaded(self, journal: Journal) -> None:
        """A crash mid-load must still leave something to restore."""
        order: list[str] = []

        class Watching(RecordingRunner):
            def __call__(self, args, *, input_text=None):  # type: ignore[no-untyped-def]
                if "-f" in args:
                    order.append(f"load(recorded={journal.load().nft_table})")
                return super().__call__(args, input_text=input_text)

        apply_rules(FirewallPlan(), journal, runner=Watching())
        assert order == ["load(recorded=True)"]

    def test_an_existing_table_is_removed_first(self, journal: Journal) -> None:
        """Loading over a table merges with it instead of replacing it."""
        runner = RecordingRunner()
        apply_rules(FirewallPlan(), journal, runner=runner)

        delete_at = next(i for i, c in enumerate(runner.calls) if "delete" in c)
        load_at = next(i for i, c in enumerate(runner.calls) if "-f" in c)
        assert delete_at < load_at

    def test_a_refused_ruleset_raises(self, journal: Journal) -> None:
        runner = RecordingRunner({"-f": Result(code=1, err="syntax error")})

        with pytest.raises(RuleLoadError, match="syntax error"):
            apply_rules(FirewallPlan(), journal, runner=runner)


class TestRejectingAlreadyResolvedAddresses:
    def test_blocked_addresses_are_rejected(self) -> None:
        """A page open before the session started must stop loading (SPEC 8.2)."""
        ruleset = build_ruleset(FirewallPlan(blocked_v4=["142.250.1.1"]))

        assert "set blocked_v4" in ruleset
        assert "ip daddr @blocked_v4 reject" in ruleset

    def test_ipv6_too(self) -> None:
        ruleset = build_ruleset(FirewallPlan(blocked_v6=["2a00:1450::1"]))
        assert "ip6 daddr @blocked_v6 reject" in ruleset

    def test_nothing_resolved_means_no_set(self) -> None:
        ruleset = build_ruleset(FirewallPlan())
        assert "blocked_v4" not in ruleset

    def test_rejecting_not_dropping(self) -> None:
        """A dropped packet leaves the page spinning; a reject fails it at once."""
        ruleset = build_ruleset(FirewallPlan(blocked_v4=["1.2.3.4"]))
        assert "@blocked_v4 reject" in ruleset
        assert "@blocked_v4 drop" not in ruleset


class TestBlockingTunnels:
    """VPN and Tor, in Strict sessions only (SPEC 8.2, ADR 4)."""

    def test_the_ports_are_blocked(self) -> None:
        ruleset = build_ruleset(
            FirewallPlan(tunnel_ports=[("udp", 51820), ("udp", 1194), ("tcp", 1723)])
        )

        assert "udp dport { 1194, 51820 } drop" in ruleset
        assert "tcp dport { 1723 } reject with tcp reset" in ruleset

    def test_tcp_is_rejected_rather_than_dropped(self) -> None:
        """A VPN client should fail and say so, not hang looking like bad wifi."""
        ruleset = build_ruleset(FirewallPlan(tunnel_ports=[("tcp", 9001)]))

        assert "reject with tcp reset" in ruleset
        assert "tcp dport { 9001 } drop" not in ruleset

    def test_nothing_is_emitted_when_tunnels_are_allowed(self) -> None:
        """Below Strict, and for a profile that opted out, there are no rules."""
        ruleset = build_ruleset(FirewallPlan(tunnel_ports=[]))
        assert "51820" not in ruleset

    def test_the_resolver_is_still_exempt(self) -> None:
        ruleset = build_ruleset(FirewallPlan(tunnel_ports=[("udp", 51820)]))
        chain = ruleset[ruleset.index("chain block_encrypted_dns") :]
        assert chain.index("meta mark") < chain.index("51820")


class TestReadingTheTunnelList:
    def test_the_shipped_list_parses(self) -> None:
        from anchor.blocker.rules import load_tunnel_ports

        shipped = Path(__file__).resolve().parents[2] / "data" / "tunnels.txt"
        ports = load_tunnel_ports(shipped)

        assert ("udp", 51820) in ports, "WireGuard"
        assert ("udp", 1194) in ports, "OpenVPN"
        assert ("udp", 500) in ports, "IPsec"
        assert ("tcp", 9001) in ports, "Tor"
        assert len(ports) >= 10

    def test_any_covers_both_protocols(self, tmp_path: Path) -> None:
        from anchor.blocker.rules import load_tunnel_ports

        listing = tmp_path / "tunnels.txt"
        listing.write_text("any/1194\n", encoding="utf-8")

        assert load_tunnel_ports(listing) == [("tcp", 1194), ("udp", 1194)]

    def test_a_bad_line_costs_one_rule_not_the_file(self, tmp_path: Path) -> None:
        from anchor.blocker.rules import load_tunnel_ports

        listing = tmp_path / "tunnels.txt"
        listing.write_text("udp/500\nnonsense\nsctp/99\nudp/abc\ntcp/1723\n", encoding="utf-8")

        assert load_tunnel_ports(listing) == [("udp", 500), ("tcp", 1723)]

    def test_an_impossible_port_is_skipped(self, tmp_path: Path) -> None:
        from anchor.blocker.rules import load_tunnel_ports

        listing = tmp_path / "tunnels.txt"
        listing.write_text("udp/0\nudp/70000\nudp/500\n", encoding="utf-8")

        assert load_tunnel_ports(listing) == [("udp", 500)]

    def test_duplicates_are_collapsed(self, tmp_path: Path) -> None:
        from anchor.blocker.rules import load_tunnel_ports

        listing = tmp_path / "tunnels.txt"
        listing.write_text("udp/500\nudp/500\n", encoding="utf-8")

        assert load_tunnel_ports(listing) == [("udp", 500)]
