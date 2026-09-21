"""What the break overlay says (SPEC 10, ADR 2, and the approved mockup)."""

from __future__ import annotations

from typing import Any

import pytest

from anchor.agent.breakscreen import (
    BreakScreen,
    format_break_countdown,
    screen_for,
)

MINUTE = 60


def status(**break_overrides: Any) -> dict[str, Any]:
    rest: dict[str, Any] = {
        "phase": "break",
        "remaining_seconds": 4 * MINUTE + 12,
        "ends_at": 1_760_000_000.0,
        "long": False,
        "taken": 0,
        "postponed": 0,
        "skipped": 0,
        "type": "overlay",
        "hardness": "moderate",
        "can_skip": False,
        "can_postpone": True,
        "allow_sites": False,
    }
    rest.update(break_overrides)
    return {
        "active": True,
        "profile": "Study",
        "level": "firm",
        "phase": rest["phase"],
        "remaining_seconds": 2 * 3600,
        "break": rest,
    }


def shown(**break_overrides: Any) -> BreakScreen:
    screen = screen_for(status(**break_overrides))
    assert screen is not None
    return screen


class TestWhenItIsShown:
    def test_not_while_working(self) -> None:
        assert screen_for(status(phase="working")) is None

    def test_not_when_no_session_is_running(self) -> None:
        assert screen_for({"active": False}) is None

    def test_not_when_the_profile_has_no_pattern(self) -> None:
        assert screen_for({"active": True, "break": None}) is None

    def test_during_a_break(self) -> None:
        assert screen_for(status()) is not None


class TestTheCountdown:
    def test_it_reads_like_the_mockup(self) -> None:
        """The mockup draws 04:12: minutes and seconds, zero-padded."""
        assert format_break_countdown(4 * MINUTE + 12) == "04:12"

    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [(0, "00:00"), (59, "00:59"), (5 * MINUTE, "05:00"), (15 * MINUTE, "15:00")],
    )
    def test_the_usual_lengths(self, seconds: int, expected: str) -> None:
        assert format_break_countdown(seconds) == expected

    def test_a_break_over_an_hour_still_reads_sensibly(self) -> None:
        assert format_break_countdown(3600 + 90) == "1:01:30"

    def test_it_never_goes_negative(self) -> None:
        assert format_break_countdown(-5) == "00:00"

    def test_it_moves_every_second_unlike_the_panel(self) -> None:
        """Watching it is most of what the screen is for."""
        assert shown(remaining_seconds=252).countdown == "04:12"
        assert shown(remaining_seconds=251).countdown == "04:11"


class TestWhatItSays:
    def test_the_title_is_the_word_from_the_mockup(self) -> None:
        assert shown().title == "BREAK"

    def test_a_long_break_says_so(self) -> None:
        assert shown(long=True).title == "LONG BREAK"

    def test_it_tells_the_user_what_to_do(self) -> None:
        assert "look away" in shown().advice

    def test_a_long_break_suggests_leaving(self) -> None:
        assert shown(long=True).advice != shown().advice

    def test_it_says_what_comes_next(self) -> None:
        assert shown().after == "Then: back to work · Study session"


class TestTheWayOut:
    def test_a_moderate_break_offers_one_postponement(self) -> None:
        screen = shown(hardness="moderate", can_postpone=True, can_skip=False)

        assert screen.postpone == "Postpone 5 min"
        assert screen.skip == ""
        assert "postponed once" in screen.footnote

    def test_and_stops_offering_it_once_it_is_used(self) -> None:
        screen = shown(hardness="moderate", can_postpone=False, can_skip=False)

        assert screen.postpone == ""
        assert "it is used" in screen.footnote

    def test_a_flexible_break_offers_both(self) -> None:
        screen = shown(hardness="flexible", can_postpone=True, can_skip=True)

        assert screen.postpone
        assert screen.skip == "Skip break"

    def test_a_mandatory_break_offers_neither(self) -> None:
        screen = shown(hardness="mandatory", can_postpone=False, can_skip=False)

        assert (screen.postpone, screen.skip) == ("", "")

    def test_and_says_why_rather_than_showing_a_blank_screen(self) -> None:
        """A screen with no way out and no explanation looks like a crash."""
        screen = shown(hardness="mandatory", can_postpone=False, can_skip=False)

        assert "cannot be skipped or postponed" in screen.footnote

    def test_a_flexible_break_that_offers_nothing_still_names_itself(self) -> None:
        """Belt and braces: the buttons are decided by the engine, not here.

        If a future rule ever leaves a Flexible break with no way out, the
        screen says what it is rather than going silent.
        """
        screen = shown(hardness="flexible", can_postpone=False, can_skip=False)

        assert screen.footnote == "Flexible breaks"
