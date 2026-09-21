"""The Home screen, as the approved mockup draws it (SPEC 14, mockup 1)."""

from __future__ import annotations

from typing import Any

import pytest

from anchor.gui.home import HomeView, home_view


def status(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "active": True,
        "session_id": "s1",
        "profile": "Study",
        "level": "firm",
        "origin": "manual",
        "valve": None,
        "phase": "working",
        "started_at": 1_760_000_000.0,
        "ends_at": 1_760_014_000.0,
        "remaining_seconds": 8077.0,
        "blocked_attempts": 7,
        "app_blocks": 0,
        "skips_remaining": 1,
        "break": {
            "phase": "working",
            "remaining_seconds": 1080.0,
            "break_seconds": 600,
            "long": False,
            "type": "overlay",
            "hardness": "moderate",
            "can_skip": False,
            "can_postpone": True,
        },
        "exit_request": None,
    }
    base.update(overrides)
    return base


def stats(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "view": "day",
        "first_day": "2026-09-21",
        "last_day": "2026-09-21",
        "focus_seconds": 6360.0,
        "sessions_completed": 1,
        "blocked_attempts": 7,
        "ruptures": 0,
        "focus_by_day": [{"day": "2026-09-21", "seconds": 6360.0}],
        "attempts_by_target": [
            {"target": "youtube.com", "kind": "domain", "count": 5},
            {"target": "reddit.com", "kind": "domain", "count": 2},
        ],
        "breaks": {"taken": 2, "postponed": 0, "skipped": 0},
        "ruptures_by_kind": {},
    }
    base.update(overrides)
    return base


def schedules(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schedules": [
            {
                "id": "a",
                "name": "Work",
                "profile": "Work",
                "days": [0, 1, 2, 3, 4],
                "start": "16:00",
                "end": "19:00",
                "level": "strict",
                "valve": "wait",
                "enabled": True,
                "active": False,
            },
            {
                "id": "b",
                "name": "Study",
                "profile": "Study",
                "days": [0, 2, 4],
                "start": "09:00",
                "end": "13:00",
                "level": "firm",
                "valve": None,
                "enabled": True,
                "active": True,
            },
        ],
        "skips_remaining": 1,
    }
    base.update(overrides)
    return base


def view(**parts: Any) -> HomeView:
    return home_view(
        parts.get("status", status()),
        schedules=parts.get("schedules", schedules()),
        stats=parts.get("stats", stats()),
        connected=parts.get("connected", True),
    )


class TestTheSessionCard:
    def test_it_names_the_profile_and_the_level(self) -> None:
        card = view().session
        assert card is not None
        assert card.profile == "Study"
        assert card.level == "Firm"

    def test_the_countdown_carries_seconds(self) -> None:
        """The panel shows minutes; this screen is being looked at (mockup 1)."""
        card = view().session
        assert card is not None
        assert card.countdown == "2:14:37"

    def test_it_says_when_the_session_ends(self) -> None:
        card = view().session
        assert card is not None
        assert "ends at" in card.remaining
        assert card.started.startswith("Started at")

    def test_the_next_break_is_when_and_how_long(self) -> None:
        card = view().session
        assert card is not None
        assert card.break_when == "in 18 min"
        assert card.break_detail == "10 min · fullscreen"

    def test_a_break_that_is_running_says_so_instead(self) -> None:
        resting = status(phase="break", **{"break": {**status()["break"], "phase": "break"}})
        card = home_view(resting, schedules=schedules(), stats=stats()).session
        assert card is not None
        assert card.break_title == "On a break"

    def test_a_profile_with_no_breaks_says_so(self) -> None:
        card = home_view(status(**{"break": None}), schedules=schedules(), stats=stats()).session
        assert card is not None
        assert card.break_when == "No breaks in this profile"

    def test_the_blocked_attempts_come_with_what_was_refused(self) -> None:
        card = view().session
        assert card is not None
        assert card.attempts == "7"
        assert card.attempts_detail == "youtube.com, reddit.com"

    def test_nothing_refused_yet_shows_no_domains(self) -> None:
        card = view(stats=stats(attempts_by_target=[])).session
        assert card is not None
        assert card.attempts_detail == ""


class TestHowToLeave:
    """SPEC 7.2 and 14: Home says what leaving costs, before it is wanted."""

    def test_soft_waits_five_minutes(self) -> None:
        card = view(status=status(level="soft")).session
        assert card is not None
        assert card.exit_how == "Wait 5 minutes"
        assert "Soft" in card.exit_rule

    def test_firm_waits_and_types(self) -> None:
        card = view().session
        assert card is not None
        assert card.exit_how == "Wait + text"

    def test_strict_has_only_its_valve(self) -> None:
        card = view(status=status(level="strict", valve="phrase")).session
        assert card is not None
        assert card.exit_how == "Random phrase"
        assert "cannot be cancelled" in card.exit_rule

    @pytest.mark.parametrize(
        ("valve", "expected"),
        [("wait", "30-minute wait"), ("phrase", "Random phrase"), ("both", "Wait and phrase")],
    )
    def test_every_valve_is_named(self, valve: str, expected: str) -> None:
        card = view(status=status(level="strict", valve=valve)).session
        assert card is not None
        assert card.exit_how == expected


class TestTheButtons:
    def test_a_soft_session_can_be_extended_or_cancelled(self) -> None:
        card = view(status=status(level="soft")).session
        assert card is not None
        assert [action.request for action in card.actions] == [
            "session.extend",
            "session.cancel",
        ]

    def test_a_strict_session_offers_the_valve_instead_of_cancel(self) -> None:
        """SPEC 14: Home replaces "Cancel session" with "Emergency valve"."""
        card = view(status=status(level="strict", valve="wait")).session
        assert card is not None
        assert [action.request for action in card.actions] == [
            "session.extend",
            "valve.request",
        ]
        assert card.actions[1].label == "Emergency valve"

    def test_a_scheduled_soft_session_can_also_be_skipped(self) -> None:
        card = view(status=status(origin="schedule", level="soft")).session
        assert card is not None
        assert "schedule.skip" in [action.request for action in card.actions]

    def test_a_scheduled_strict_session_cannot(self) -> None:
        card = view(status=status(origin="schedule", level="strict", valve="wait")).session
        assert card is not None
        assert "schedule.skip" not in [action.request for action in card.actions]

    def test_the_skip_is_offered_but_disabled_with_none_left(self) -> None:
        """Hiding it would look like the feature does not exist (SPEC 11)."""
        card = view(status=status(origin="schedule", level="soft", skips_remaining=0)).session
        assert card is not None
        skip = [action for action in card.actions if action.request == "schedule.skip"][0]
        assert not skip.enabled

    def test_a_pending_exit_offers_to_withdraw_it(self) -> None:
        pending = status(
            exit_request={"kind": "cancel", "remaining_wait_seconds": 300.0, "phrase": None}
        )
        card = view(status=pending).session
        assert card is not None
        assert "session.withdraw_cancel" in [action.request for action in card.actions]
        assert "5 min" in card.exit_pending


class TestWithNoSessionRunning:
    """The mockup only draws an active session; the screen still has to exist."""

    def test_there_is_no_card(self) -> None:
        assert view(status={"active": False, "skips_remaining": 3}).session is None

    def test_starting_one_is_the_thing_offered(self) -> None:
        idle = view(status={"active": False, "skips_remaining": 3}).idle
        assert idle is not None
        assert idle.action.request == "session.start"

    def test_the_schedules_and_the_figures_are_still_there(self) -> None:
        home = view(status={"active": False, "skips_remaining": 3})
        assert home.schedules
        assert home.today


class TestTheRestOfTheScreen:
    def test_the_schedules_are_the_next_ones_not_all_of_them(self) -> None:
        home = view()
        assert [line.name for line in home.schedules] == ["Study", "Work"]

    def test_the_one_running_now_says_so(self) -> None:
        running = [line for line in view().schedules if line.name == "Study"][0]
        assert running.now

    def test_a_disabled_schedule_is_left_out(self) -> None:
        listing = schedules()
        listing["schedules"][0]["enabled"] = False
        assert [line.name for line in view(schedules=listing).schedules] == ["Study"]

    def test_the_skips_left_this_week_are_shown(self) -> None:
        assert view().skips == "Skips this week: 2 of 3"

    def test_today_is_focus_breaks_and_attempts(self) -> None:
        figures = view().today
        assert [figure.value for figure in figures] == ["1 h 46 min", "2", "7"]
        assert figures[0].label == "of focus"


class TestWhenTheEngineIsNotThere:
    def test_the_screen_says_so_rather_than_showing_stale_numbers(self) -> None:
        home = view(connected=False)
        assert "cannot reach" in home.banner

    def test_and_keeps_the_card_it_last_had(self) -> None:
        """Blanking it would read as "your session ended", which is a lie."""
        home = view(connected=False)
        assert home.session is not None
