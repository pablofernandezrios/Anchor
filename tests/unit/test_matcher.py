"""Deciding whether a name is blocked (SPEC 8.1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from anchor.blocker.matcher import (
    Policy,
    load_companions,
    load_domain_file,
    normalise_domain,
    with_companions,
)
from anchor.protocol.types import WebMode

ROOT = Path(__file__).resolve().parents[2]


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


class TestAnAllowedSiteArrivesWhole:
    """SPEC 8.1: a permitted site must load what it is made of.

    The owner put github.com on an allowlist and got a column of unstyled
    links, because GitHub keeps its stylesheets on githubassets.com and that
    is a different domain. Anchor sees DNS names and nothing else, so it
    cannot infer that one query was made on behalf of another -- it can only
    be told in advance, which is what these two lists are.
    """

    def policy(self, *domains: str) -> Policy:
        return Policy(
            mode=WebMode.ALLOWLIST,
            domains=with_companions(
                frozenset(domains), {"github.com": frozenset({"githubassets.com"})}
            ),
            assets=frozenset({"fonts.googleapis.com", "cdnjs.cloudflare.com"}),
        )

    def test_the_site_itself_resolves(self) -> None:
        assert not self.policy("github.com").is_blocked("github.com")

    def test_and_so_does_what_it_keeps_its_stylesheets_on(self) -> None:
        assert not self.policy("github.com").is_blocked("github.githubassets.com")

    def test_a_companion_of_a_site_you_did_not_allow_stays_blocked(self) -> None:
        """The conditionality is the point: this is not a second allowlist."""
        assert self.policy("wikipedia.org").is_blocked("github.githubassets.com")

    def test_shared_infrastructure_resolves(self) -> None:
        assert not self.policy("github.com").is_blocked("fonts.googleapis.com")

    def test_but_it_does_not_open_the_web(self) -> None:
        """You reach a distraction by its name, and its name still fails."""
        policy = self.policy("github.com")

        assert policy.is_blocked("reddit.com")
        assert policy.is_blocked("youtube.com")


class TestTheAssetListIsNotALoophole:
    """In blocklist mode the user named what to block, and that stands."""

    def test_a_blocked_site_stays_blocked_though_assets_are_known(self) -> None:
        policy = Policy(
            mode=WebMode.BLOCKLIST,
            domains=frozenset({"fonts.googleapis.com"}),
            assets=frozenset({"fonts.googleapis.com"}),
        )

        assert policy.is_blocked("fonts.googleapis.com")


class TestReadingTheCompanionList:
    def test_a_site_and_its_companions(self, tmp_path: Path) -> None:
        path = tmp_path / "companions.txt"
        path.write_text(
            "# a comment\ngithub.com: githubassets.com githubusercontent.com\n",
            encoding="utf-8",
        )

        assert load_companions(path) == {
            "github.com": frozenset({"githubassets.com", "githubusercontent.com"})
        }

    def test_a_line_with_no_companions_is_skipped(self, tmp_path: Path) -> None:
        path = tmp_path / "companions.txt"
        path.write_text("github.com:\ngitlab.com: gitlab-static.net\n", encoding="utf-8")

        assert load_companions(path) == {"gitlab.com": frozenset({"gitlab-static.net"})}

    def test_a_bad_entry_costs_itself_and_not_the_file(self, tmp_path: Path) -> None:
        """A typo in shipped data must not take the whole list down."""
        path = tmp_path / "companions.txt"
        path.write_text("///: nonsense\ngitlab.com: gitlab-static.net\n", encoding="utf-8")

        assert load_companions(path) == {"gitlab.com": frozenset({"gitlab-static.net"})}

    def test_a_missing_file_is_empty_rather_than_fatal(self, tmp_path: Path) -> None:
        assert load_companions(tmp_path / "nothing.txt") == {}

    def test_companions_do_not_have_companions(self) -> None:
        """One hop. A chain would allow a great deal by naming one thing."""
        chained = {
            "a.example": frozenset({"b.example"}),
            "b.example": frozenset({"c.example"}),
        }
        expanded = with_companions(frozenset({"a.example"}), chained)

        assert "b.example" in expanded
        assert "c.example" not in expanded


class TestTheShippedCompanionList:
    """The file this repository ships, read by the code that reads it."""

    def test_it_parses(self) -> None:
        companions = load_companions(ROOT / "data" / "companions.txt")

        assert companions, "the shipped companion list is empty"

    def test_github_brings_what_the_owner_needed(self) -> None:
        companions = load_companions(ROOT / "data" / "companions.txt")

        assert "githubassets.com" in companions["github.com"]
        assert "githubusercontent.com" in companions["github.com"]

    def test_the_asset_list_parses_too(self) -> None:
        assets = load_domain_file(ROOT / "data" / "web-assets.txt")

        assert "fonts.googleapis.com" in assets
        assert "cdnjs.cloudflare.com" in assets
