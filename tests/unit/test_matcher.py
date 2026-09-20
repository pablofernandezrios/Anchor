"""Deciding whether a name is blocked (SPEC 8.1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from anchor.blocker.matcher import Policy, normalise_domain
from anchor.protocol.types import WebMode


def blocklist(*domains: str, essentials: frozenset[str] | None = None) -> Policy:
    return Policy(
        mode=WebMode.BLOCKLIST,
        domains=frozenset(domains),
        essentials=essentials if essentials is not None else frozenset(),
    )


def allowlist(*domains: str, essentials: frozenset[str] | None = None) -> Policy:
    return Policy(
        mode=WebMode.ALLOWLIST,
        domains=frozenset(domains),
        essentials=essentials if essentials is not None else frozenset(),
    )


class TestNormalising:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Example.COM", "example.com"),
            ("example.com.", "example.com"),
            ("  example.com  ", "example.com"),
            ("https://example.com/path", "example.com"),
            ("http://www.example.com", "www.example.com"),
            ("example.com:8443", "example.com"),
            ("www.example.com/watch?v=1", "www.example.com"),
        ],
    )
    def test_what_people_type_becomes_a_domain(self, raw: str, expected: str) -> None:
        """A rule is a domain, but people paste whole URLs (SPEC 8.1)."""
        assert normalise_domain(raw) == expected

    def test_an_international_domain_becomes_punycode(self) -> None:
        assert normalise_domain("münchen.de") == "xn--mnchen-3ya.de"

    def test_nonsense_is_rejected(self) -> None:
        for raw in ("", "   ", "https://", "..", "."):
            with pytest.raises(ValueError, match="not a domain"):
                normalise_domain(raw)


class TestBlocklist:
    def test_a_listed_domain_is_blocked(self) -> None:
        assert blocklist("youtube.com").is_blocked("youtube.com")

    def test_subdomains_are_blocked_too(self) -> None:
        """SPEC 8.1: a rule covers all subdomains."""
        policy = blocklist("youtube.com")
        assert policy.is_blocked("www.youtube.com")
        assert policy.is_blocked("m.youtube.com")
        assert policy.is_blocked("a.b.c.youtube.com")

    def test_anything_else_is_allowed(self) -> None:
        policy = blocklist("youtube.com")
        assert not policy.is_blocked("example.com")
        assert not policy.is_blocked("wikipedia.org")

    def test_a_name_that_merely_ends_in_the_rule_is_not_a_subdomain(self) -> None:
        """notyoutube.com is a different site from youtube.com."""
        policy = blocklist("youtube.com")
        assert not policy.is_blocked("notyoutube.com")
        assert not policy.is_blocked("myyoutube.com")

    def test_the_rule_is_not_a_substring_match(self) -> None:
        policy = blocklist("bbc.co.uk")
        assert not policy.is_blocked("bbc.co.uk.evil.com")

    def test_case_does_not_help(self) -> None:
        assert blocklist("youtube.com").is_blocked("WWW.YouTube.COM")

    def test_a_trailing_dot_does_not_help(self) -> None:
        assert blocklist("youtube.com").is_blocked("www.youtube.com.")

    def test_an_empty_list_blocks_nothing(self) -> None:
        assert not blocklist().is_blocked("anything.com")


class TestAllowlist:
    def test_a_listed_domain_is_allowed(self) -> None:
        assert not allowlist("wikipedia.org").is_blocked("wikipedia.org")

    def test_subdomains_of_a_listed_domain_are_allowed(self) -> None:
        assert not allowlist("wikipedia.org").is_blocked("en.wikipedia.org")

    def test_everything_else_is_blocked(self) -> None:
        """SPEC 8.1: an allowlist blocks everything it does not name."""
        policy = allowlist("wikipedia.org")
        assert policy.is_blocked("youtube.com")
        assert policy.is_blocked("example.com")

    def test_an_empty_allowlist_blocks_the_whole_web(self) -> None:
        assert allowlist().is_blocked("example.com")


class TestEssentials:
    def test_essentials_survive_an_allowlist(self) -> None:
        """SPEC 8.1: the system has to keep working."""
        policy = allowlist("wikipedia.org", essentials=frozenset({"ntp.ubuntu.com"}))
        assert not policy.is_blocked("ntp.ubuntu.com")

    def test_essentials_cover_their_subdomains(self) -> None:
        policy = allowlist(essentials=frozenset({"pool.ntp.org"}))
        assert not policy.is_blocked("0.pool.ntp.org")


class TestSpecialUseNames:
    @pytest.mark.parametrize(
        "name",
        [
            "localhost",
            "printer.local",
            "1.0.0.127.in-addr.arpa",
            "router.home.arpa",
            "",
        ],
    )
    def test_special_names_are_never_blocked(self, name: str) -> None:
        """Blocking these would break the machine, not a distraction."""
        assert not allowlist().is_blocked(name)

    def test_a_site_merely_ending_in_local_is_still_blocked(self) -> None:
        assert allowlist().is_blocked("notlocal.com")


class TestReporting:
    def test_the_matching_rule_is_named(self) -> None:
        """The statistics record which rule caught a name (SPEC 13)."""
        policy = blocklist("youtube.com")
        assert policy.matching_rule("www.youtube.com") == "youtube.com"

    def test_an_allowed_name_matches_no_rule(self) -> None:
        assert blocklist("youtube.com").matching_rule("example.com") is None

    def test_the_most_specific_rule_wins(self) -> None:
        policy = blocklist("example.com", "ads.example.com")
        assert policy.matching_rule("ads.example.com") == "ads.example.com"


class TestLoadingDomainFiles:
    def test_comments_and_blanks_are_ignored(self, tmp_path: Path) -> None:
        from anchor.blocker.matcher import load_domain_file

        listing = tmp_path / "essentials.txt"
        listing.write_text(
            "# a comment\n\n  \nexample.com\nother.org  # trailing note\n",
            encoding="utf-8",
        )

        assert load_domain_file(listing) == frozenset({"example.com", "other.org"})

    def test_a_bad_line_costs_one_entry_not_the_file(self, tmp_path: Path) -> None:
        from anchor.blocker.matcher import load_domain_file

        listing = tmp_path / "essentials.txt"
        listing.write_text("good.com\n...\nalso-good.org\n", encoding="utf-8")

        assert load_domain_file(listing) == frozenset({"good.com", "also-good.org"})

    def test_a_missing_file_is_empty_rather_than_fatal(self, tmp_path: Path) -> None:
        from anchor.blocker.matcher import load_domain_file

        assert load_domain_file(tmp_path / "nope.txt") == frozenset()

    def test_the_shipped_essentials_list_loads(self) -> None:
        """The file that ships with Anchor has to be readable (SPEC 8.1)."""
        from anchor.blocker.matcher import load_domain_file

        shipped = Path(__file__).resolve().parents[2] / "data" / "essentials.txt"
        essentials = load_domain_file(shipped)

        assert "connectivity-check.ubuntu.com" in essentials
        assert "pool.ntp.org" in essentials
        assert len(essentials) >= 8
