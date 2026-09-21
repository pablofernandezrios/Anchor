"""What `anchor doctor` concludes from what it finds (SPEC 15).

Gathering the facts touches nftables, systemd-resolved and /etc; judging them
does not, and this is the judging.
"""

from __future__ import annotations

from typing import Any

import pytest

from anchor.engine.doctor import BrowserFact, Facts, examine, worst


def check(facts: Facts, name: str) -> Any:
    found = [one for one in examine(facts) if one.name == name]
    assert found, f"no check called {name!r}"
    return found[0]


def healthy(**overrides: Any) -> Facts:
    """A machine with nothing wrong with it, outside a session."""
    base: dict[str, Any] = {
        "session_active": False,
        "blocker_last_seen_seconds": 1.0,
        "nftables_usable": True,
        "table_loaded": False,
        "upstreams": ("192.168.1.1",),
        "resolved_available": True,
        "resolved_drop_in": False,
        "browsers": (BrowserFact(name="Firefox", installed=True, policy_present=False),),
        "indicator": True,
    }
    base.update(overrides)
    return Facts(**base)


class TestAHealthyMachine:
    def test_nothing_is_wrong(self) -> None:
        assert worst(examine(healthy())) is None

    def test_every_thing_the_spec_names_is_checked(self) -> None:
        # SPEC 15: DNS path, nftables table, browser policies, daemons,
        # indicator extension.
        names = {one.name for one in examine(healthy())}
        assert names == {"blocker", "dns", "nftables", "browsers", "indicator"}

    def test_the_checks_say_what_they_found_not_just_that_they_passed(self) -> None:
        assert "192.168.1.1" in check(healthy(), "dns").detail


class TestTheBlocker:
    def test_a_blocker_that_has_never_checked_in_is_the_worst_news_there_is(self) -> None:
        result = check(healthy(blocker_last_seen_seconds=None), "blocker")

        assert not result.ok
        assert "nothing is being blocked" in result.detail.lower()
        assert "systemctl" in result.fix

    def test_a_blocker_that_stopped_answering_is_reported_too(self) -> None:
        result = check(healthy(blocker_last_seen_seconds=120.0), "blocker")

        assert not result.ok
        assert "2 min" in result.detail

    def test_a_slow_poll_is_not_a_failure(self) -> None:
        # It polls once a second; a few seconds late is a busy machine.
        assert check(healthy(blocker_last_seen_seconds=8.0), "blocker").ok


class TestTheDnsPath:
    def test_no_upstream_means_anchor_would_refuse_to_redirect(self) -> None:
        result = check(healthy(upstreams=()), "dns")

        assert not result.ok
        # Which is the right refusal: redirecting with nowhere to forward to
        # would take every name on the machine down.
        assert "resolve" in result.detail.lower()

    def test_during_a_session_the_drop_in_must_be_in_place(self) -> None:
        result = check(healthy(session_active=True, resolved_drop_in=False), "dns")

        assert not result.ok
        assert "systemd-resolved" in result.detail

    def test_during_a_session_with_the_drop_in_all_is_well(self) -> None:
        assert check(healthy(session_active=True, resolved_drop_in=True), "dns").ok

    def test_a_machine_without_systemd_resolved_uses_the_other_path(self) -> None:
        result = check(
            healthy(session_active=True, resolved_available=False, resolved_drop_in=False), "dns"
        )

        assert result.ok
        assert "resolv.conf" in result.detail

    def test_outside_a_session_the_drop_in_should_be_gone(self) -> None:
        result = check(healthy(resolved_drop_in=True), "dns")

        assert not result.ok
        assert "no session" in result.detail.lower()


class TestTheFirewall:
    def test_without_nftables_anchor_cannot_block_anything(self) -> None:
        result = check(healthy(nftables_usable=False), "nftables")

        assert not result.ok
        assert "nftables" in result.fix

    def test_during_a_session_the_table_must_be_loaded(self) -> None:
        result = check(healthy(session_active=True, table_loaded=False), "nftables")

        assert not result.ok

    def test_during_a_session_a_loaded_table_is_what_should_happen(self) -> None:
        assert check(healthy(session_active=True, table_loaded=True), "nftables").ok

    def test_a_table_left_behind_by_a_crash_is_reported(self) -> None:
        """The one that matters: rules blocking a machine with no session."""
        result = check(healthy(table_loaded=True), "nftables")

        assert not result.ok
        assert "--restore" in result.fix


class TestBrowsers:
    def test_during_a_session_an_installed_browser_needs_its_policy(self) -> None:
        result = check(
            healthy(
                session_active=True,
                browsers=(BrowserFact(name="Firefox", installed=True, policy_present=False),),
            ),
            "browsers",
        )

        assert not result.ok
        assert "Firefox" in result.detail

    def test_a_browser_that_is_not_installed_is_not_a_problem(self) -> None:
        result = check(
            healthy(
                session_active=True,
                browsers=(
                    BrowserFact(name="Firefox", installed=True, policy_present=True),
                    BrowserFact(name="Brave", installed=False, policy_present=False),
                ),
            ),
            "browsers",
        )

        assert result.ok
        assert "Brave" not in result.detail

    def test_policies_left_behind_by_a_crash_are_reported(self) -> None:
        result = check(
            healthy(browsers=(BrowserFact(name="Firefox", installed=True, policy_present=True),)),
            "browsers",
        )

        assert not result.ok
        assert "--restore" in result.fix

    def test_no_browser_at_all_is_worth_saying_out_loud(self) -> None:
        result = check(healthy(browsers=()), "browsers")

        assert result.ok
        assert "no browser" in result.detail.lower()


class TestTheIndicator:
    def test_a_missing_extension_explains_how_to_install_it(self) -> None:
        result = check(healthy(indicator=False), "indicator")

        assert not result.ok
        assert "appindicator" in result.fix.lower()

    def test_a_missing_extension_does_not_mean_anchor_is_broken(self) -> None:
        # SPEC 14.1: the time left is still on Home and in `anchor status`.
        result = check(healthy(indicator=False), "indicator")

        assert not result.blocking
        assert worst(examine(healthy(indicator=False))) == "note"

    def test_a_check_nobody_could_run_says_so(self) -> None:
        """The engine is root and has no session bus; only a client can look."""
        result = check(healthy(indicator=None), "indicator")

        assert result.ok
        assert "Could not be checked" in result.detail


class TestTheVerdict:
    def test_a_problem_outranks_a_note(self) -> None:
        assert worst(examine(healthy(indicator=False, upstreams=()))) == "problem"

    @pytest.mark.parametrize("facts", [healthy(), healthy(blocker_last_seen_seconds=0.0)])
    def test_a_clean_machine_has_no_verdict_at_all(self, facts: Facts) -> None:
        assert worst(examine(facts)) is None
