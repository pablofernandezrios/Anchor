"""The Statistics screen (SPEC 13, mockup 6)."""

from __future__ import annotations

from typing import Any

from anchor.gui.stats import stats_view


def summary(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "view": "week",
        "first_day": "2026-09-21",
        "last_day": "2026-09-27",
        "focus_seconds": 76_320.0,
        "sessions_completed": 14,
        "blocked_attempts": 96,
        "ruptures": 1,
        "focus_by_day": [
            {"day": "2026-09-21", "seconds": 3.5 * 3600},
            {"day": "2026-09-22", "seconds": 4 * 3600},
            {"day": "2026-09-23", "seconds": 4.5 * 3600},
            {"day": "2026-09-24", "seconds": 2.5 * 3600},
            {"day": "2026-09-25", "seconds": 3.2 * 3600},
            {"day": "2026-09-26", "seconds": 2 * 3600},
            {"day": "2026-09-27", "seconds": 1.5 * 3600},
        ],
        "attempts_by_target": [
            {"target": "youtube.com", "kind": "domain", "count": 41},
            {"target": "reddit.com", "kind": "domain", "count": 22},
            {"target": "discord.desktop", "kind": "app", "count": 9},
        ],
        "breaks": {"taken": 23, "postponed": 5, "skipped": 0},
        "ruptures_by_kind": {"valve": 0, "skip": 1, "tampering": 0},
    }
    base.update(overrides)
    return base


def view(**overrides: Any) -> Any:
    settings: dict[str, Any] = {"summary": summary(), "apps": {"discord.desktop": "Discord"}}
    settings.update(overrides)
    return stats_view(**settings)


class TestTheRange:
    def test_all_three_views_are_offered(self) -> None:
        assert [one.key for one in view().ranges] == ["day", "week", "month"]

    def test_the_chosen_one_is_known(self) -> None:
        assert view().range == "week"

    def test_the_period_is_written_out(self) -> None:
        assert view().period == "21–27 September 2026"

    def test_a_single_day_is_written_as_one_date(self) -> None:
        one = view(summary=summary(view="day", last_day="2026-09-21"))
        assert one.period == "21 September 2026"

    def test_a_period_that_crosses_a_month_says_both(self) -> None:
        across = view(summary=summary(first_day="2026-09-28", last_day="2026-10-04"))
        assert across.period == "28 September – 4 October 2026"


class TestTheHeadline:
    def test_it_is_the_four_numbers_the_mockup_shows(self) -> None:
        assert [one.label for one in view().headline] == [
            "Focus hours",
            "Sessions completed",
            "Blocked attempts",
            "Ruptures",
        ]

    def test_the_focus_time_reads_the_way_anchor_writes_durations(self) -> None:
        assert view().headline[0].value == "21 h 12 min"

    def test_the_counts_are_plain_numbers(self) -> None:
        assert [one.value for one in view().headline[1:]] == ["14", "96", "1"]


class TestTheChart:
    def test_there_is_a_bar_for_each_day(self) -> None:
        assert len(view().bars) == 7

    def test_the_days_are_labelled(self) -> None:
        assert [one.label for one in view().bars] == [
            "Mon",
            "Tue",
            "Wed",
            "Thu",
            "Fri",
            "Sat",
            "Sun",
        ]

    def test_a_month_labels_by_the_day_of_the_month(self) -> None:
        """Thirty weekday names in a row would say nothing at all."""
        month = view(
            summary=summary(
                view="month",
                first_day="2026-09-01",
                last_day="2026-09-30",
                focus_by_day=[
                    {"day": "2026-09-01", "seconds": 3600},
                    {"day": "2026-09-02", "seconds": 7200},
                ],
            )
        )
        assert [one.label for one in month.bars] == ["1", "2"]

    def test_the_tallest_bar_fills_the_chart(self) -> None:
        assert max(one.fraction for one in view().bars) == 1.0

    def test_the_others_are_in_proportion(self) -> None:
        monday = view().bars[0]
        assert monday.fraction == 3.5 / 4.5

    def test_a_week_with_no_focus_at_all_draws_flat(self) -> None:
        """And does not divide by zero doing it."""
        empty = view(summary=summary(focus_by_day=[{"day": "2026-09-21", "seconds": 0}]))
        assert empty.bars[0].fraction == 0.0

    def test_each_bar_says_its_hours(self) -> None:
        assert view().bars[0].value == "3.5"


class TestWhatWasRefused:
    def test_the_ranked_list_is_in_order(self) -> None:
        assert [one.target for one in view().attempts] == [
            "youtube.com",
            "reddit.com",
            "Discord",
        ]

    def test_an_application_is_marked_as_one(self) -> None:
        """Because "Discord" and "discord.com" are different things to block."""
        discord = view().attempts[2]
        assert discord.kind == "app"
        assert discord.label == "Discord (app)"

    def test_an_application_with_no_desktop_entry_keeps_its_identifier(self) -> None:
        assert view(apps={}).attempts[2].target == "discord.desktop"

    def test_the_bars_are_relative_to_the_most_refused(self) -> None:
        assert [one.fraction for one in view().attempts] == [1.0, 22 / 41, 9 / 41]

    def test_nothing_refused_says_so(self) -> None:
        assert view(summary=summary(attempts_by_target=[])).attempts_empty


class TestBreaksAndRuptures:
    def test_breaks_are_taken_postponed_and_skipped(self) -> None:
        assert [(one.value, one.label) for one in view().breaks] == [
            ("23", "taken"),
            ("5", "postponed"),
            ("0", "skipped"),
        ]

    def test_ruptures_are_named_by_what_they_were(self) -> None:
        assert [(one.value, one.label) for one in view().ruptures] == [
            ("0", "valves used"),
            ("1", "schedules skipped"),
            ("0", "tampering"),
        ]

    def test_a_kind_that_never_happened_is_still_shown_as_zero(self) -> None:
        """A missing row reads as "not measured", which is a different claim."""
        none = view(summary=summary(ruptures_by_kind={}))
        assert [one.value for one in none.ruptures] == ["0", "0", "0"]


class TestDeleting:
    def test_the_one_action_is_offered_with_what_it_costs(self) -> None:
        """SPEC 13: one action deletes everything, and nothing restores it."""
        assert "cannot be undone" in view().delete_warning
        assert view().delete_label
