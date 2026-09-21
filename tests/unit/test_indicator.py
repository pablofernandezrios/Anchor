"""What the top bar says (SPEC 14.1)."""

from __future__ import annotations

from typing import Any

import pytest

from anchor.agent.indicator import (
    ICON_ACTIVE,
    ICON_UNKNOWN,
    UNKNOWN_LABEL,
    IndicatorModel,
    format_clock,
    format_label,
)

HOUR = 3600


def status(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "active": True,
        "session_id": "abc",
        "profile": "Study",
        "level": "firm",
        "origin": "manual",
        "valve": None,
        "phase": "working",
        "started_at": 1_760_000_000.0,
        "ends_at": 1_760_000_000.0 + 2 * HOUR,
        "remaining_seconds": 2 * HOUR + 14 * 60,
        "blocked_attempts": 0,
        "app_blocks": 0,
        "skips_remaining": 3,
    }
    base.update(overrides)
    return base


def model(**overrides: Any) -> IndicatorModel:
    subject = IndicatorModel()
    subject.update_status(status(**overrides))
    return subject


class TestTheLabel:
    def test_it_reads_like_the_mockup(self) -> None:
        """The approved design shows 2:14, not 2:14:37."""
        assert format_label(2 * HOUR + 14 * 60) == "2:14"

    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (0, "0:00"),
            (45 * 60, "0:45"),
            (HOUR, "1:00"),
            (8 * HOUR, "8:00"),
        ],
    )
    def test_the_usual_times(self, seconds: int, expected: str) -> None:
        assert format_label(seconds) == expected

    def test_minutes_round_up(self) -> None:
        """Otherwise the last minute reads 0:00 for a full sixty seconds."""
        assert format_label(1) == "0:01"
        assert format_label(59) == "0:01"
        assert format_label(61) == "0:02"

    def test_a_session_that_just_started_shows_its_whole_length(self) -> None:
        assert format_label(2 * HOUR - 0.3) == "2:00"

    def test_a_negative_countdown_is_not_shown_as_negative(self) -> None:
        assert format_label(-30) == "0:00"

    def test_the_label_appears_in_the_view(self) -> None:
        assert model().view.label == "2:14"


class TestWhenNothingIsRunning:
    def test_the_indicator_is_not_shown(self) -> None:
        """An icon that sits there all day teaches the user to ignore it."""
        subject = IndicatorModel()
        subject.update_status({"active": False, "skips_remaining": 3})

        assert subject.view.visible is False

    def test_an_empty_status_shows_nothing(self) -> None:
        assert IndicatorModel().view.visible is False


class TestDuringASession:
    def test_it_is_shown(self) -> None:
        assert model().view.visible is True

    def test_the_icon_is_the_session_icon(self) -> None:
        assert model().view.icon == ICON_ACTIVE

    def test_the_tooltip_names_the_profile_and_the_level(self) -> None:
        assert model().view.tooltip == "Study · Firm · 2:14 left"

    def test_the_guide_keeps_the_panel_from_jumping(self) -> None:
        assert model().view.guide == "0:00"


class TestTheMenu:
    def labels(self, subject: IndicatorModel) -> list[str]:
        return [item.label for item in subject.view.menu]

    def test_it_opens_with_the_profile_and_level(self) -> None:
        assert self.labels(model())[0] == "Study · Firm"

    def test_it_gives_the_time_left_and_the_end_time(self) -> None:
        line = self.labels(model())[1]

        assert line.startswith("2:14 left · ends at ")

    def test_it_shows_the_blocked_attempts(self) -> None:
        """SPEC 8.3: the indicator carries the count for the session."""
        assert "Blocked attempts: 7" in self.labels(model(blocked_attempts=7))

    def test_closed_applications_appear_once_there_are_any(self) -> None:
        assert "Applications closed: 2" in self.labels(model(app_blocks=2))

    def test_and_are_left_out_when_there_are_none(self) -> None:
        """A line reading zero is a line that teaches nothing."""
        assert not any("Applications closed" in label for label in self.labels(model()))

    def test_a_break_says_so(self) -> None:
        assert "On a break" in self.labels(model(phase="break"))

    def test_the_two_actions_are_the_ones_the_specification_names(self) -> None:
        actions = [item.action for item in model().view.menu if item.action]

        assert actions == ["extend", "open"]

    def test_the_information_lines_are_not_clickable(self) -> None:
        for item in model().view.menu:
            if not item.action:
                assert not item.enabled


class TestWhenTheEngineIsUnreachable:
    def test_a_running_session_stays_visible(self) -> None:
        """Disappearing would say the session ended, which may be a lie."""
        subject = model()
        subject.note_connection(False)

        assert subject.view.visible is True

    def test_the_time_is_not_invented(self) -> None:
        """Counting down from memory would fake the one number people trust."""
        subject = model()
        subject.note_connection(False)

        assert subject.view.label == UNKNOWN_LABEL
        assert subject.view.icon == ICON_UNKNOWN

    def test_it_says_blocking_is_still_in_place(self) -> None:
        subject = model()
        subject.note_connection(False)

        assert any("stays in place" in item.label for item in subject.view.menu)

    def test_with_no_session_there_is_nothing_to_say(self) -> None:
        subject = IndicatorModel()
        subject.update_status({"active": False})
        subject.note_connection(False)

        assert subject.view.visible is False

    def test_reconnecting_restores_the_countdown(self) -> None:
        subject = model()
        subject.note_connection(False)
        subject.note_connection(True)

        assert subject.view.label == "2:14"


class TestFollowingEvents:
    def test_a_tick_moves_the_countdown(self) -> None:
        subject = model()
        subject.note_event("session.tick", status(remaining_seconds=HOUR))

        assert subject.view.label == "1:00"

    def test_a_session_ending_hides_it(self) -> None:
        """This event carries no status, so it has to be understood."""
        subject = model()
        subject.note_event(
            "session.ended", {"session_id": "abc", "profile": "Study", "reason": "completed"}
        )

        assert subject.view.visible is False

    def test_a_session_starting_shows_it(self) -> None:
        subject = IndicatorModel()
        subject.update_status({"active": False})
        subject.note_event("session.started", status())

        assert subject.view.visible is True
        assert subject.view.label == "2:14"

    def test_an_event_without_a_status_changes_nothing(self) -> None:
        """A blocked attempt does not carry the session; the next tick does."""
        subject = model()
        subject.note_event("blocked.attempt", {"domain": "youtube.com", "rule": "youtube.com"})

        assert subject.view.label == "2:14"
        assert subject.view.visible is True


class TestTheClock:
    def test_it_reads_as_hours_and_minutes(self) -> None:
        assert len(format_clock(1_760_000_000.0)) == 5

    def test_a_nonsense_timestamp_does_not_crash_the_panel(self) -> None:
        assert format_clock(1e30) == "?"
