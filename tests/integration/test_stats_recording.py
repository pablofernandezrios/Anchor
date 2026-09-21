"""What actually reaches the statistics database (SPEC 13).

The database is tested on its own. This is about the engine using it: that a
session, a refused domain, a closed application, a break and a rupture each
leave the right mark, and that a broken database never takes a session down
with it.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from anchor.engine.core import Engine
from anchor.engine.paths import Paths, Settings
from anchor.engine.profiles import BreakSettings, Profile
from anchor.engine.timekeeping import FakeClock
from anchor.protocol.messages import Request
from anchor.protocol.types import BreakHardness, WebMode

HOUR = 3600
MINUTE = 60


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(wall_time=1_760_000_000.0, boottime=1_000.0, boot="boot-a")


@pytest.fixture
def engine(tmp_path: Path, clock: FakeClock) -> Engine:
    paths = Paths.resolve(tmp_path)
    paths.ensure_directories()
    engine = Engine(
        paths,
        Settings(owner_uid=os.getuid()),
        clock=clock,
        systemd_runtime_dir=tmp_path / "run" / "systemd" / "system",
    )
    engine.load()
    engine.profiles["Study"] = Profile(
        name="Study",
        web_mode=WebMode.BLOCKLIST,
        domains=frozenset({"youtube.com"}),
        breaks=BreakSettings(work_minutes=25, break_minutes=5, hardness=BreakHardness.FLEXIBLE),
    )
    engine.save_config()
    return engine


def start(engine: Engine, level: str = "firm") -> str:
    response = engine.handle(
        Request(
            type="session.start",
            payload={
                "profile": "Study",
                "duration_seconds": 2 * HOUR,
                "level": level,
                "origin": "manual",
                "valve": None,
            },
        )
    )
    assert response.ok, response.error
    return str(response.result["session_id"])


def rows(engine: Engine, query: str) -> list[Any]:
    return engine.stats.rows(query)


class TestSessions:
    def test_a_session_is_recorded_when_it_starts(self, engine: Engine) -> None:
        session_id = start(engine)

        recorded = rows(engine, "SELECT * FROM sessions")
        assert len(recorded) == 1
        assert recorded[0]["id"] == session_id
        assert recorded[0]["profile"] == "Study"

    def test_a_finished_session_counts_as_completed(self, engine: Engine, clock: FakeClock) -> None:
        start(engine)
        clock.advance(2 * HOUR + MINUTE)
        engine.tick()

        assert rows(engine, "SELECT completed FROM sessions")[0]["completed"] == 1

    def test_its_focus_lands_in_the_day_it_happened(self, engine: Engine, clock: FakeClock) -> None:
        start(engine)
        clock.advance(2 * HOUR + MINUTE)
        engine.tick()

        focus = rows(engine, "SELECT SUM(seconds) AS total FROM focus")[0]["total"]
        assert focus == pytest.approx(2 * HOUR, abs=90)

    def test_a_cancelled_session_is_recorded_but_not_completed(
        self, engine: Engine, clock: FakeClock
    ) -> None:
        start(engine, level="soft")
        engine.handle(Request(type="session.cancel", payload={"typed": None}))
        clock.advance(10 * MINUTE)
        engine.tick()

        recorded = rows(engine, "SELECT completed, reason FROM sessions")[0]
        assert recorded["completed"] == 0
        assert recorded["reason"] == "cancel"


class TestBlockedAttempts:
    def test_a_refused_domain_is_recorded(self, engine: Engine) -> None:
        start(engine)

        engine.handle(
            Request(
                type="blocked.report",
                payload={"domain": "youtube.com", "rule": "youtube.com"},
            )
        )

        recorded = rows(engine, "SELECT kind, target FROM attempts")[0]
        assert (recorded["kind"], recorded["target"]) == ("domain", "youtube.com")

    def test_a_closed_application_is_recorded_as_an_application(self, engine: Engine) -> None:
        """SPEC 13 asks for attempts by domain and by app, kept apart."""
        start(engine)

        engine.handle(
            Request(
                type="apps.report", payload={"kind": "launch", "apps": ["Discord"], "seconds": 0}
            )
        )

        recorded = rows(engine, "SELECT kind, target FROM attempts")[0]
        assert (recorded["kind"], recorded["target"]) == ("app", "Discord")

    def test_several_applications_at_once_are_several_rows(self, engine: Engine) -> None:
        start(engine)

        engine.handle(
            Request(
                type="apps.report",
                payload={"kind": "closed", "apps": ["Discord", "Steam"], "seconds": 0},
            )
        )

        assert len(rows(engine, "SELECT * FROM attempts")) == 2


class TestBreaks:
    def test_a_break_taken_is_recorded(self, engine: Engine, clock: FakeClock) -> None:
        start(engine)
        for _ in range(62):
            clock.advance(30)
            engine.tick()

        outcomes = [row["outcome"] for row in rows(engine, "SELECT outcome FROM breaks")]
        assert "taken" in outcomes

    def test_a_break_nobody_saw_is_still_recorded(self, engine: Engine, clock: FakeClock) -> None:
        """An absence covering a break publishes no event, and still counts."""
        start(engine)
        clock.advance(40 * MINUTE)
        engine.tick()

        assert [row["outcome"] for row in rows(engine, "SELECT outcome FROM breaks")] == ["taken"]

    def test_skipping_is_recorded_as_skipping(self, engine: Engine, clock: FakeClock) -> None:
        start(engine)
        for _ in range(51):
            clock.advance(30)
            engine.tick()

        engine.handle(Request(type="break.skip", payload={}))

        assert "skipped" in [row["outcome"] for row in rows(engine, "SELECT outcome FROM breaks")]

    def test_postponing_is_recorded_as_postponing(self, engine: Engine, clock: FakeClock) -> None:
        start(engine)
        for _ in range(51):
            clock.advance(30)
            engine.tick()

        engine.handle(Request(type="break.postpone", payload={}))

        assert "postponed" in [row["outcome"] for row in rows(engine, "SELECT outcome FROM breaks")]


class TestRuptures:
    def test_tampering_is_recorded(self, engine: Engine) -> None:
        start(engine)

        engine.handle(
            Request(
                type="tamper.report",
                payload={"kind": "rules_missing", "detail": "the table was removed"},
            )
        )

        assert rows(engine, "SELECT kind FROM ruptures")[0]["kind"] == "tampering"

    def test_a_clock_that_moved_is_recorded(self, engine: Engine, clock: FakeClock) -> None:
        start(engine)
        clock.warp_wall_clock(-3600)
        engine.tick()

        assert rows(engine, "SELECT kind FROM ruptures")[0]["kind"] == "tampering"


class TestRetention:
    def test_old_statistics_are_swept_when_a_session_ends(
        self, engine: Engine, clock: FakeClock
    ) -> None:
        engine.stats.attempt(
            session_id="ancient", kind="domain", target="old.example", at=clock.wall() - 200 * 86400
        )
        start(engine)
        clock.advance(2 * HOUR + MINUTE)
        engine.tick()

        remaining = [row["target"] for row in rows(engine, "SELECT target FROM attempts")]
        assert "old.example" not in remaining

    def test_the_window_comes_from_the_settings(self, tmp_path: Path, clock: FakeClock) -> None:
        paths = Paths.resolve(tmp_path)
        paths.ensure_directories()
        engine = Engine(
            paths,
            Settings(owner_uid=os.getuid(), retention_days=7),
            clock=clock,
            systemd_runtime_dir=tmp_path / "run" / "systemd" / "system",
        )

        assert engine.stats.retention_days == 7


class TestWhenTheDatabaseIsBroken:
    def test_a_session_still_starts(self, engine: Engine, tmp_path: Path) -> None:
        """A statistic is a nice-to-have; a session is not (P4)."""
        engine.stats.path.write_bytes(b"this is not a database")
        engine.stats._ready = False  # noqa: SLF001

        session_id = start(engine)

        assert engine.state.session is not None
        assert engine.state.session.id == session_id

    def test_and_still_ends(self, engine: Engine, clock: FakeClock) -> None:
        start(engine)
        engine.stats.path.write_bytes(b"this is not a database")
        engine.stats._ready = False  # noqa: SLF001

        clock.advance(2 * HOUR + MINUTE)
        engine.tick()

        assert engine.state.session is None


class TestThePrivacyRule:
    def test_domains_reach_the_database_and_nowhere_else(
        self, engine: Engine, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The working rules allow domains here and forbid them in the log."""
        start(engine)

        with caplog.at_level("DEBUG", logger="anchord"):
            engine.handle(
                Request(
                    type="blocked.report",
                    payload={"domain": "private-site.example", "rule": "private-site.example"},
                )
            )

        assert "private-site" not in caplog.text
        with sqlite3.connect(engine.stats.path) as connection:
            stored = connection.execute("SELECT target FROM attempts").fetchone()
        assert stored[0] == "private-site.example"
