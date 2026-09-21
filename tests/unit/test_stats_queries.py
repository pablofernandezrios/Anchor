"""The day, week and month views (SPEC 13, and the approved mockup).

The mockup's statistics page draws four headline numbers, a bar per day, a
ranked list of what was refused, and the break and rupture counts. These tests
describe that page.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

from anchor.engine.stats import Statistics, summarise

HOUR = 3600
TODAY = date(2026, 9, 23)  # a Wednesday


def at(day: date, hour: int = 12, minute: int = 0) -> float:
    return datetime(day.year, day.month, day.day, hour, minute).timestamp()


@pytest.fixture
def stats(tmp_path: Path) -> Statistics:
    return Statistics(tmp_path / "stats.db")


def a_session(
    stats: Statistics,
    *,
    session_id: str,
    day: date,
    hours: float = 2.0,
    reason: str = "completed",
) -> None:
    started = at(day, 9)
    ended = started + hours * HOUR
    stats.session_started(
        session_id=session_id, profile="Study", level="firm", origin="manual", at=started
    )
    stats.session_ended(session_id=session_id, started_at=started, ended_at=ended, reason=reason)


class TestTheHeadlineNumbers:
    def test_focus_hours_add_up(self, stats: Statistics) -> None:
        a_session(stats, session_id="s1", day=TODAY, hours=2)
        a_session(stats, session_id="s2", day=TODAY, hours=1.5)

        summary = summarise(stats, "day", today=TODAY)

        assert summary.focus_seconds == pytest.approx(3.5 * HOUR)

    def test_only_completed_sessions_are_counted_as_completed(self, stats: Statistics) -> None:
        """SPEC 13 says sessions completed, which is not sessions started."""
        a_session(stats, session_id="s1", day=TODAY)
        a_session(stats, session_id="s2", day=TODAY, reason="cancel")

        assert summarise(stats, "day", today=TODAY).sessions_completed == 1

    def test_a_cancelled_session_still_counts_its_focus(self, stats: Statistics) -> None:
        """The hour you worked before giving up was still an hour worked."""
        a_session(stats, session_id="s1", day=TODAY, hours=1, reason="cancel")

        assert summarise(stats, "day", today=TODAY).focus_seconds == pytest.approx(HOUR)

    def test_blocked_attempts_are_totalled(self, stats: Statistics) -> None:
        for name in ("youtube.com", "youtube.com", "reddit.com"):
            stats.attempt(session_id="s1", kind="domain", target=name, at=at(TODAY))

        assert summarise(stats, "day", today=TODAY).blocked_attempts == 3

    def test_ruptures_are_totalled(self, stats: Statistics) -> None:
        stats.rupture(session_id="s1", kind="valve", at=at(TODAY))
        stats.rupture(session_id="s1", kind="tampering", at=at(TODAY))

        assert summarise(stats, "day", today=TODAY).ruptures == 2


class TestTheBarChart:
    def test_a_week_has_seven_bars(self, stats: Statistics) -> None:
        """Monday to Sunday, as the mockup draws it."""
        summary = summarise(stats, "week", today=TODAY)

        assert len(summary.focus_by_day) == 7
        assert summary.first_day == "2026-09-21"
        assert summary.last_day == "2026-09-27"

    def test_empty_days_are_still_days(self, stats: Statistics) -> None:
        """A chart with days missing lies about the shape of a week."""
        a_session(stats, session_id="s1", day=TODAY, hours=2)

        summary = summarise(stats, "week", today=TODAY)

        assert [seconds for _day, seconds in summary.focus_by_day] == [
            0.0,
            0.0,
            2 * HOUR,
            0.0,
            0.0,
            0.0,
            0.0,
        ]

    def test_a_month_has_a_bar_for_every_day(self, stats: Statistics) -> None:
        assert len(summarise(stats, "month", today=TODAY).focus_by_day) == 30

    def test_a_day_has_one(self, stats: Statistics) -> None:
        assert len(summarise(stats, "day", today=TODAY).focus_by_day) == 1

    def test_a_session_over_midnight_shows_in_both_days(self, stats: Statistics) -> None:
        started = at(date(2026, 9, 22), 23)
        stats.session_started(
            session_id="s1", profile="Study", level="firm", origin="manual", at=started
        )
        stats.session_ended(
            session_id="s1", started_at=started, ended_at=started + 2 * HOUR, reason="completed"
        )

        summary = summarise(stats, "week", today=TODAY)
        by_day = dict(summary.focus_by_day)

        assert by_day["2026-09-22"] == pytest.approx(HOUR)
        assert by_day["2026-09-23"] == pytest.approx(HOUR)


class TestWhatWasRefused:
    def test_the_list_is_ranked(self, stats: Statistics) -> None:
        for _ in range(3):
            stats.attempt(session_id="s", kind="domain", target="youtube.com", at=at(TODAY))
        stats.attempt(session_id="s", kind="domain", target="reddit.com", at=at(TODAY))

        ranked = summarise(stats, "day", today=TODAY).attempts_by_target

        assert [tally.target for tally in ranked] == ["youtube.com", "reddit.com"]
        assert ranked[0].count == 3

    def test_applications_appear_in_the_same_list_and_say_so(self, stats: Statistics) -> None:
        """The mockup lists "Discord (app)" among the domains."""
        stats.attempt(session_id="s", kind="app", target="Discord", at=at(TODAY))

        tally = summarise(stats, "day", today=TODAY).attempts_by_target[0]

        assert (tally.target, tally.kind) == ("Discord", "app")

    def test_a_domain_and_an_application_of_the_same_name_stay_apart(
        self, stats: Statistics
    ) -> None:
        stats.attempt(session_id="s", kind="domain", target="discord.com", at=at(TODAY))
        stats.attempt(session_id="s", kind="app", target="discord.com", at=at(TODAY))

        ranked = summarise(stats, "day", today=TODAY).attempts_by_target

        assert len(ranked) == 2
        assert {tally.kind for tally in ranked} == {"domain", "app"}

    def test_the_list_is_capped_but_the_total_is_not(self, stats: Statistics) -> None:
        for index in range(25):
            stats.attempt(
                session_id="s", kind="domain", target=f"site{index}.example", at=at(TODAY)
            )

        summary = summarise(stats, "day", today=TODAY)

        assert len(summary.attempts_by_target) == 10
        assert summary.blocked_attempts == 25

    def test_ties_are_broken_by_name_rather_than_by_chance(self, stats: Statistics) -> None:
        """A list that reshuffles itself between two identical days is noise."""
        for name in ("b.example", "a.example"):
            stats.attempt(session_id="s", kind="domain", target=name, at=at(TODAY))

        ranked = summarise(stats, "day", today=TODAY).attempts_by_target

        assert [tally.target for tally in ranked] == ["a.example", "b.example"]


class TestBreaksAndRuptures:
    def test_the_three_break_outcomes_are_reported(self, stats: Statistics) -> None:
        """SPEC 13: taken, postponed and skipped."""
        for outcome, times in (("taken", 3), ("postponed", 2), ("skipped", 1)):
            for _ in range(times):
                stats.break_outcome(session_id="s", outcome=outcome, at=at(TODAY))

        assert dict(summarise(stats, "day", today=TODAY).breaks) == {
            "taken": 3,
            "postponed": 2,
            "skipped": 1,
        }

    def test_all_three_appear_even_at_zero(self, stats: Statistics) -> None:
        """The mockup shows 0 saltados, which is a number worth seeing."""
        assert dict(summarise(stats, "day", today=TODAY).breaks) == {
            "taken": 0,
            "postponed": 0,
            "skipped": 0,
        }

    def test_the_three_rupture_kinds_are_reported(self, stats: Statistics) -> None:
        stats.rupture(session_id="s", kind="valve", at=at(TODAY))
        stats.rupture(session_id="s", kind="skip", at=at(TODAY))

        assert dict(summarise(stats, "day", today=TODAY).ruptures_by_kind) == {
            "valve": 1,
            "skip": 1,
            "tampering": 0,
        }


class TestTheBoundaries:
    def test_yesterday_is_not_today(self, stats: Statistics) -> None:
        a_session(stats, session_id="s1", day=date(2026, 9, 22))

        assert summarise(stats, "day", today=TODAY).focus_seconds == 0

    def test_last_week_is_not_this_week(self, stats: Statistics) -> None:
        stats.attempt(session_id="s", kind="domain", target="old.example", at=at(date(2026, 9, 20)))

        assert summarise(stats, "week", today=TODAY).blocked_attempts == 0

    def test_monday_belongs_to_its_own_week(self, stats: Statistics) -> None:
        stats.attempt(
            session_id="s", kind="domain", target="a.example", at=at(date(2026, 9, 21), 0, 1)
        )

        assert summarise(stats, "week", today=TODAY).blocked_attempts == 1

    def test_the_last_minute_of_sunday_is_still_the_week(self, stats: Statistics) -> None:
        stats.attempt(
            session_id="s", kind="domain", target="a.example", at=at(date(2026, 9, 27), 23, 59)
        )

        assert summarise(stats, "week", today=TODAY).blocked_attempts == 1

    def test_next_month_is_not_this_month(self, stats: Statistics) -> None:
        stats.attempt(
            session_id="s", kind="domain", target="a.example", at=at(date(2026, 10, 1), 0, 1)
        )

        assert summarise(stats, "month", today=TODAY).blocked_attempts == 0


class TestAnEmptyDatabase:
    def test_it_answers_with_zeroes_rather_than_nothing(self, stats: Statistics) -> None:
        """A first run has no statistics, and the page still has to draw."""
        summary = summarise(stats, "week", today=TODAY)

        assert summary.focus_seconds == 0
        assert summary.sessions_completed == 0
        assert summary.attempts_by_target == ()
        assert len(summary.focus_by_day) == 7

    def test_a_broken_database_does_not_raise(self, tmp_path: Path) -> None:
        rubbish = tmp_path / "stats.db"
        rubbish.write_bytes(b"this is not a database")

        summary = summarise(Statistics(rubbish), "week", today=TODAY)

        assert summary.blocked_attempts == 0


class TestTheWireShape:
    def test_it_is_plain_json(self, stats: Statistics) -> None:
        import json

        a_session(stats, session_id="s1", day=TODAY)
        stats.attempt(session_id="s1", kind="app", target="Discord", at=at(TODAY))

        as_dict = summarise(stats, "day", today=TODAY).to_dict()

        assert json.loads(json.dumps(as_dict))["attempts_by_target"][0]["kind"] == "app"

    def test_it_carries_the_range_it_answered(self, stats: Statistics) -> None:
        """A client drawing "21–27 Sep" should not have to work it out again."""
        as_dict = summarise(stats, "week", today=TODAY).to_dict()

        assert (as_dict["first_day"], as_dict["last_day"]) == ("2026-09-21", "2026-09-27")
