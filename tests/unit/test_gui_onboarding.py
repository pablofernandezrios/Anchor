"""First run: what it explains, and the live test it ends with (SPEC 14)."""

from __future__ import annotations

import pytest

from anchor.gui.onboarding import (
    CONTROL_DOMAIN,
    SAMPLE_DOMAIN,
    STEPS,
    TEST_PROFILE,
    TEST_SECONDS,
    cleanup_requests,
    finished_request,
    setup_requests,
    verdict_for,
)


class TestWhatItExplains:
    def test_it_covers_the_three_things_the_spec_names(self) -> None:
        keys = [step.key for step in STEPS]
        for wanted in ("levels", "valve", "allowlist"):
            assert wanted in keys

    def test_it_ends_with_the_live_test(self) -> None:
        assert STEPS[-1].key == "test"

    def test_the_levels_step_says_what_each_one_costs(self) -> None:
        levels = [step for step in STEPS if step.key == "levels"][0]
        body = " ".join(levels.points)
        assert "5 minutes" in body
        assert "cannot be cancelled" in body

    def test_the_valve_step_says_every_use_is_recorded(self) -> None:
        """SPEC 7.5. Someone learning about the escape hatch should hear it."""
        valve = [step for step in STEPS if step.key == "valve"][0]
        assert "recorded" in " ".join(valve.points)

    def test_the_allowlist_step_carries_the_warning_spec_8_1_asks_for(self) -> None:
        allowlist = [step for step in STEPS if step.key == "allowlist"][0]
        assert "other domains" in " ".join(allowlist.points)

    def test_every_step_has_something_to_read(self) -> None:
        assert all(step.title and (step.body or step.points) for step in STEPS)


class TestTheLiveTest:
    def test_it_blocks_a_domain_nobody_is_using(self) -> None:
        """example.com is reserved by IANA for exactly this."""
        assert SAMPLE_DOMAIN == "example.com"
        assert CONTROL_DOMAIN == "example.org"

    def test_it_builds_a_profile_of_its_own_and_starts_a_short_session(self) -> None:
        requests = setup_requests()

        assert [type_ for type_, _payload in requests] == [
            "profile.create",
            "session.start",
        ]
        create, start = (payload for _type, payload in requests)
        assert create["domains"] == [SAMPLE_DOMAIN]
        assert create["apps"] == []
        assert start["profile"] == TEST_PROFILE
        assert start["duration_seconds"] == TEST_SECONDS

    def test_the_session_is_soft_and_short_enough_to_wait_out(self) -> None:
        """No bypass exists and none is wanted: it ends by expiring.

        A Strict test session would be a trap, and an exit that skipped the
        friction would be the one hole Anchor must not have.
        """
        _create, start = (payload for _type, payload in setup_requests())
        assert start["level"] == "soft"
        assert TEST_SECONDS <= 60

    def test_the_test_profile_has_no_breaks_in_the_way(self) -> None:
        create, _start = (payload for _type, payload in setup_requests())
        assert create["breaks"]["work_minutes"] * 60 > TEST_SECONDS

    def test_afterwards_the_profile_is_taken_away_again(self) -> None:
        assert cleanup_requests() == [("profile.delete", {"name": TEST_PROFILE})]

    def test_finishing_remembers_that_it_ran(self) -> None:
        assert finished_request() == (
            "config.set",
            {"key": "onboarding_done", "value": "yes"},
        )


class TestTheVerdict:
    def test_blocked_and_the_control_still_working_is_a_pass(self) -> None:
        result = verdict_for(sample_resolves=False, control_resolves=True)

        assert result.ok
        assert SAMPLE_DOMAIN in result.detail

    def test_the_sample_still_resolving_is_a_failure(self) -> None:
        result = verdict_for(sample_resolves=True, control_resolves=True)

        assert not result.ok
        assert "anchor doctor" in result.fix

    def test_neither_resolving_proves_nothing(self) -> None:
        """A machine with no network blocks everything, Anchor or not."""
        result = verdict_for(sample_resolves=False, control_resolves=False)

        assert not result.ok
        assert not result.failed
        assert "network" in result.detail.lower()

    def test_and_that_is_not_the_same_as_a_failure(self) -> None:
        inconclusive = verdict_for(sample_resolves=False, control_resolves=False)
        failure = verdict_for(sample_resolves=True, control_resolves=True)

        assert failure.failed
        assert not inconclusive.failed

    @pytest.mark.parametrize(
        ("sample", "control"), [(True, False), (True, True), (False, True), (False, False)]
    )
    def test_there_is_always_something_to_say(self, sample: bool, control: bool) -> None:
        result = verdict_for(sample_resolves=sample, control_resolves=control)
        assert result.title and result.detail


class TestTheIndicatorStep:
    def test_a_missing_extension_is_explained_rather_than_hidden(self) -> None:
        """SPEC 14.1 asks onboarding to detect its absence and say what to do."""
        from anchor.gui.onboarding import indicator_step

        step = indicator_step(present=False)
        assert "appindicator" in " ".join(step.points).lower()

    def test_a_working_one_is_confirmed_in_a_line(self) -> None:
        from anchor.gui.onboarding import indicator_step

        assert indicator_step(present=True).points

    def test_a_check_that_could_not_run_does_not_claim_it_failed(self) -> None:
        from anchor.gui.onboarding import indicator_step

        step = indicator_step(present=None)
        assert "could not" in " ".join(step.points).lower()


class TestTheRequests:
    def test_nothing_in_the_live_test_touches_the_user_s_own_profiles(self) -> None:
        names = {
            payload.get("name") or payload.get("profile")
            for _type, payload in [*setup_requests(), *cleanup_requests()]
        }
        assert names == {TEST_PROFILE}
