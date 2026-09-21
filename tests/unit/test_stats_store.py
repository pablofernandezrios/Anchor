"""The statistics database (SPEC 13)."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path

import pytest

from anchor.engine.stats import Statistics, range_bounds, split_by_day

DAY = 86400


def at(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> float:
    return datetime(year, month, day, hour, minute).timestamp()


@pytest.fixture
def stats(tmp_path: Path) -> Statistics:
    return Statistics(tmp_path / "stats.db")


class TestSplittingFocusByDay:
    def test_an_ordinary_session_is_one_day(self) -> None:
        buckets = split_by_day(at(2026, 9, 21, 9), at(2026, 9, 21, 11, 30))

        assert buckets == [("2026-09-21", 2.5 * 3600)]

    def test_a_session_over_midnight_is_two(self) -> None:
        """Otherwise one day is overstated and the next reads empty."""
        buckets = split_by_day(at(2026, 9, 21, 23), at(2026, 9, 22, 1))

        assert [day for day, _ in buckets] == ["2026-09-21", "2026-09-22"]
        assert [seconds for _, seconds in buckets] == [3600, 3600]

    def test_a_very_long_session_covers_every_day_it_touches(self) -> None:
        buckets = split_by_day(at(2026, 9, 21, 22), at(2026, 9, 24, 2))

        assert len(buckets) == 4
        assert sum(seconds for _, seconds in buckets) == pytest.approx(2 * DAY + 4 * 3600)

    def test_a_session_with_no_length_is_nothing(self) -> None:
        assert split_by_day(at(2026, 9, 21, 9), at(2026, 9, 21, 9)) == []

    def test_time_running_backwards_is_not_negative_focus(self) -> None:
        assert split_by_day(at(2026, 9, 21, 11), at(2026, 9, 21, 9)) == []


class TestRecording:
    def test_a_session_is_written_when_it_starts(self, stats: Statistics) -> None:
        """So a session in progress is visible, not only a finished one."""
        stats.session_started(
            session_id="s1", profile="Study", level="firm", origin="manual", at=at(2026, 9, 21, 9)
        )

        rows = stats.rows("SELECT * FROM sessions")
        assert len(rows) == 1
        assert rows[0]["profile"] == "Study"
        assert rows[0]["ended_at"] is None

    def test_and_closed_when_it_ends(self, stats: Statistics) -> None:
        stats.session_started(
            session_id="s1", profile="Study", level="firm", origin="manual", at=at(2026, 9, 21, 9)
        )

        stats.session_ended(
            session_id="s1",
            started_at=at(2026, 9, 21, 9),
            ended_at=at(2026, 9, 21, 11),
            reason="completed",
        )

        row = stats.rows("SELECT * FROM sessions")[0]
        assert row["completed"] == 1
        assert row["reason"] == "completed"

    def test_a_cancelled_session_is_not_completed(self, stats: Statistics) -> None:
        """SPEC 13 counts sessions completed, which is not sessions started."""
        stats.session_started(
            session_id="s1", profile="Study", level="soft", origin="manual", at=at(2026, 9, 21, 9)
        )
        stats.session_ended(
            session_id="s1",
            started_at=at(2026, 9, 21, 9),
            ended_at=at(2026, 9, 21, 10),
            reason="cancel",
        )

        assert stats.rows("SELECT * FROM sessions")[0]["completed"] == 0

    def test_focus_lands_in_the_days_it_covered(self, stats: Statistics) -> None:
        stats.session_started(
            session_id="s1", profile="Study", level="firm", origin="manual", at=at(2026, 9, 21, 23)
        )
        stats.session_ended(
            session_id="s1",
            started_at=at(2026, 9, 21, 23),
            ended_at=at(2026, 9, 22, 1),
            reason="completed",
        )

        rows = stats.rows("SELECT day, seconds FROM focus ORDER BY day")
        assert [row["day"] for row in rows] == ["2026-09-21", "2026-09-22"]

    def test_ending_a_session_twice_does_not_double_its_focus(self, stats: Statistics) -> None:
        """The engine ticks; an end that arrives twice must not count twice."""
        for _ in range(2):
            stats.session_ended(
                session_id="s1",
                started_at=at(2026, 9, 21, 9),
                ended_at=at(2026, 9, 21, 11),
                reason="completed",
            )

        assert len(stats.rows("SELECT * FROM focus")) == 1

    def test_a_blocked_domain_is_recorded(self, stats: Statistics) -> None:
        stats.attempt(session_id="s1", kind="domain", target="youtube.com", at=at(2026, 9, 21))

        assert stats.rows("SELECT * FROM attempts")[0]["target"] == "youtube.com"

    def test_a_blocked_application_is_recorded_apart(self, stats: Statistics) -> None:
        """SPEC 13 wants attempts by domain AND by app, so the kind is kept."""
        stats.attempt(session_id="s1", kind="app", target="Discord", at=at(2026, 9, 21))

        assert stats.rows("SELECT kind FROM attempts")[0]["kind"] == "app"

    def test_breaks_are_recorded_with_their_outcome(self, stats: Statistics) -> None:
        for outcome in ("taken", "postponed", "skipped"):
            stats.break_outcome(session_id="s1", outcome=outcome, at=at(2026, 9, 21))

        assert len(stats.rows("SELECT * FROM breaks")) == 3

    def test_ruptures_are_recorded_by_kind(self, stats: Statistics) -> None:
        stats.rupture(session_id="s1", kind="valve", at=at(2026, 9, 21))

        assert stats.rows("SELECT kind FROM ruptures")[0]["kind"] == "valve"


class TestRetention:
    def fill(self, stats: Statistics, *, days_ago: int, now: float) -> None:
        when = now - days_ago * DAY
        stats.attempt(session_id="s", kind="domain", target="old.example", at=when)
        stats.break_outcome(session_id="s", outcome="taken", at=when)
        stats.rupture(session_id="s", kind="valve", at=when)
        stats.session_started(
            session_id=f"s{days_ago}", profile="P", level="soft", origin="manual", at=when
        )
        stats.session_ended(
            session_id=f"s{days_ago}", started_at=when, ended_at=when + 3600, reason="completed"
        )

    def test_old_rows_are_forgotten(self, stats: Statistics) -> None:
        now = at(2026, 9, 21, 12)
        self.fill(stats, days_ago=200, now=now)

        stats.prune(now=now)

        assert stats.rows("SELECT * FROM attempts") == []
        assert stats.rows("SELECT * FROM focus") == []
        assert stats.rows("SELECT * FROM sessions") == []

    def test_recent_rows_are_kept(self, stats: Statistics) -> None:
        now = at(2026, 9, 21, 12)
        self.fill(stats, days_ago=10, now=now)

        stats.prune(now=now)

        assert len(stats.rows("SELECT * FROM attempts")) == 1

    def test_the_window_is_configurable(self, tmp_path: Path) -> None:
        """SPEC 13: configurable, default 90 days."""
        stats = Statistics(tmp_path / "s.db", retention_days=7)
        now = at(2026, 9, 21, 12)
        self.fill(stats, days_ago=10, now=now)

        stats.prune(now=now)

        assert stats.rows("SELECT * FROM attempts") == []

    def test_a_running_session_is_never_pruned(self, stats: Statistics) -> None:
        """It has no end yet, and it is the session you are in."""
        now = at(2026, 9, 21, 12)
        stats.session_started(
            session_id="live", profile="P", level="soft", origin="manual", at=now - 200 * DAY
        )

        stats.prune(now=now)

        assert len(stats.rows("SELECT * FROM sessions")) == 1


class TestDeletingEverything:
    def test_one_action_empties_it(self, stats: Statistics) -> None:
        """SPEC 13 asks for exactly this, and it must leave nothing."""
        stats.session_started(
            session_id="s1", profile="P", level="soft", origin="manual", at=at(2026, 9, 21)
        )
        stats.session_ended(
            session_id="s1",
            started_at=at(2026, 9, 21, 9),
            ended_at=at(2026, 9, 21, 10),
            reason="completed",
        )
        stats.attempt(session_id="s1", kind="domain", target="youtube.com", at=at(2026, 9, 21))
        stats.break_outcome(session_id="s1", outcome="taken", at=at(2026, 9, 21))
        stats.rupture(session_id="s1", kind="valve", at=at(2026, 9, 21))

        stats.delete_everything()

        for table in ("sessions", "focus", "attempts", "breaks", "ruptures"):
            assert stats.rows(f"SELECT * FROM {table}") == [], table

    def test_the_database_still_works_afterwards(self, stats: Statistics) -> None:
        stats.delete_everything()

        stats.attempt(session_id="s2", kind="domain", target="x.com", at=at(2026, 9, 21))

        assert len(stats.rows("SELECT * FROM attempts")) == 1


class TestWhenTheDatabaseIsBroken:
    def test_a_write_that_fails_does_not_raise(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A statistic is a nice-to-have. A session is not (P4)."""
        rubbish = tmp_path / "stats.db"
        rubbish.write_bytes(b"this is not a database")
        stats = Statistics(rubbish)

        with caplog.at_level("WARNING", logger="anchord"):
            stats.attempt(session_id="s", kind="domain", target="a.com", at=1.0)

        assert "could not record" in caplog.text

    def test_no_kind_of_write_raises(self, tmp_path: Path) -> None:
        """Every writer, not only the one that happened to be tested first."""
        rubbish = tmp_path / "stats.db"
        rubbish.write_bytes(b"this is not a database")
        stats = Statistics(rubbish)

        stats.session_started(session_id="s", profile="P", level="soft", origin="manual", at=1.0)
        stats.session_ended(session_id="s", started_at=1.0, ended_at=2.0, reason="completed")
        stats.attempt(session_id="s", kind="domain", target="a.com", at=1.0)
        stats.break_outcome(session_id="s", outcome="taken", at=1.0)
        stats.rupture(session_id="s", kind="valve", at=1.0)
        stats.delete_everything()

        assert stats.prune(now=1.0) == 0

    def test_a_write_that_fails_halfway_is_survived(self, stats: Statistics) -> None:
        """The database opened and then the statement failed, which is worse."""
        stats.attempt(session_id="s", kind="domain", target="a.com", at=1.0)
        with sqlite3.connect(stats.path) as connection:
            connection.execute("DROP TABLE attempts")

        stats.attempt(session_id="s", kind="domain", target="b.com", at=2.0)

        assert stats.rows("SELECT name FROM sqlite_master WHERE name = 'attempts'") == []

    def test_a_read_that_fails_returns_nothing(self, tmp_path: Path) -> None:
        rubbish = tmp_path / "stats.db"
        rubbish.write_bytes(b"this is not a database")

        assert Statistics(rubbish).rows("SELECT * FROM attempts") == []

    def test_a_directory_that_does_not_exist_is_made(self, tmp_path: Path) -> None:
        stats = Statistics(tmp_path / "deep" / "deeper" / "stats.db")

        stats.attempt(session_id="s", kind="domain", target="a.com", at=1.0)

        assert len(stats.rows("SELECT * FROM attempts")) == 1


class TestTheRanges:
    def test_a_day_is_that_day(self) -> None:
        assert range_bounds("day", date(2026, 9, 21)) == (date(2026, 9, 21), date(2026, 9, 21))

    def test_a_week_runs_monday_to_sunday(self) -> None:
        """The mockup draws L M X J V S D, so it is the calendar week."""
        first, last = range_bounds("week", date(2026, 9, 23))

        assert first == date(2026, 9, 21)
        assert last == date(2026, 9, 27)
        assert first.weekday() == 0

    def test_a_week_asked_for_on_a_sunday_is_that_week(self) -> None:
        assert range_bounds("week", date(2026, 9, 27))[0] == date(2026, 9, 21)

    def test_a_month_is_the_calendar_month(self) -> None:
        assert range_bounds("month", date(2026, 9, 21)) == (date(2026, 9, 1), date(2026, 9, 30))

    def test_february_in_a_leap_year(self) -> None:
        assert range_bounds("month", date(2028, 2, 10))[1] == date(2028, 2, 29)

    def test_december_rolls_into_january(self) -> None:
        assert range_bounds("month", date(2026, 12, 5))[1] == date(2026, 12, 31)

    def test_an_unknown_range_is_refused(self) -> None:
        with pytest.raises(ValueError, match="unknown range"):
            range_bounds("fortnight", date(2026, 9, 21))


class TestTheSchema:
    def test_it_is_made_once_and_reused(self, stats: Statistics) -> None:
        stats.attempt(session_id="s", kind="domain", target="a.com", at=1.0)
        stats.attempt(session_id="s", kind="domain", target="b.com", at=2.0)

        assert len(stats.rows("SELECT * FROM attempts")) == 2

    def test_an_existing_database_is_not_rebuilt(self, tmp_path: Path) -> None:
        """The engine restarts; the statistics do not start again."""
        path = tmp_path / "stats.db"
        first = Statistics(path)
        first.attempt(session_id="s", kind="domain", target="a.com", at=1.0)

        second = Statistics(path)

        assert len(second.rows("SELECT * FROM attempts")) == 1

    def test_it_is_ordinary_sqlite(self, stats: Statistics) -> None:
        """SPEC 13 says SQLite, so it must be readable as one."""
        stats.attempt(session_id="s", kind="domain", target="a.com", at=1.0)

        with sqlite3.connect(stats.path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 1
