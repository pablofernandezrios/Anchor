"""The nftables ruleset Anchor loads (SPEC 8.2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from anchor.blocker.commands import RecordingRunner, Result
from anchor.blocker.journal import Journal
from anchor.blocker.rules import (
    FirewallPlan,
    RuleLoadError,
    apply_rules,
    build_ruleset,
    load_addresses,
    split_addresses,
)


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
