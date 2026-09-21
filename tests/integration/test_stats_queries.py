"""`anchor stats`, over the socket, against a real engine (SPEC 13, 15)."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from anchor.cli.client import EngineClient
from anchor.engine.core import Engine
from anchor.engine.paths import Paths, Settings
from anchor.engine.profiles import Profile
from anchor.engine.service import EngineServer
from anchor.engine.timekeeping import FakeClock

HOUR = 3600


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    tree = Paths.resolve(tmp_path)
    tree.ensure_directories()
    return tree


@pytest.fixture
def clock() -> FakeClock:
    # A Wednesday, so a week has days on both sides of it.
    return FakeClock(wall_time=1_790_000_000.0, boottime=1_000.0, boot="boot-a")


@pytest.fixture
def engine(paths: Paths, clock: FakeClock, tmp_path: Path) -> Engine:
    engine = Engine(
        paths,
        Settings(owner_uid=os.getuid()),
        clock=clock,
        systemd_runtime_dir=tmp_path / "run" / "systemd" / "system",
    )
    engine.load()
    engine.profiles["Study"] = Profile(name="Study", domains=frozenset({"youtube.com"}))
    engine.save_config()
    return engine


@pytest.fixture
def client(engine: Engine, paths: Paths) -> Iterator[EngineClient]:
    server = EngineServer(engine)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with EngineClient(paths.engine_socket, timeout=5.0) as client:
            yield client
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def a_finished_session(engine: Engine, clock: FakeClock, hours: float = 2.0) -> None:
    from anchor.protocol.messages import Request

    engine.handle(
        Request(
            type="session.start",
            payload={
                "profile": "Study",
                "duration_seconds": hours * HOUR,
                "level": "soft",
                "origin": "manual",
                "valve": None,
            },
        )
    )
    engine.handle(
        Request(type="blocked.report", payload={"domain": "youtube.com", "rule": "youtube.com"})
    )
    clock.advance(hours * HOUR + 60)
    engine.tick()


def cli(paths: Paths, *arguments: str, stdin: str = "") -> tuple[int, str, str]:
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "anchor.cli.main",
            "--root",
            str(paths.state_dir.parents[2]),
            *arguments,
        ],
        capture_output=True,
        text=True,
        input=stdin,
        check=False,
    )
    return done.returncode, done.stdout, done.stderr


class TestAskingTheEngine:
    def test_a_week_is_the_default(self, client: EngineClient) -> None:
        result = client.call("stats.query").result

        assert result["view"] == "week"
        assert len(result["focus_by_day"]) == 7

    def test_a_finished_session_shows_up(
        self, client: EngineClient, engine: Engine, clock: FakeClock
    ) -> None:
        a_finished_session(engine, clock)

        result = client.call("stats.query", {"range": "day"}).result

        assert result["sessions_completed"] == 1
        assert result["focus_seconds"] == pytest.approx(2 * HOUR, abs=120)

    def test_so_does_a_blocked_domain(
        self, client: EngineClient, engine: Engine, clock: FakeClock
    ) -> None:
        a_finished_session(engine, clock)

        result = client.call("stats.query", {"range": "week"}).result

        assert result["attempts_by_target"][0]["target"] == "youtube.com"

    def test_an_unknown_range_is_refused(self, client: EngineClient) -> None:
        response = client.call("stats.query", {"range": "fortnight"})

        assert not response.ok

    def test_the_day_comes_from_the_engine(self, client: EngineClient, clock: FakeClock) -> None:
        """Two clients asking at once must not disagree about what day it is."""
        first = client.call("stats.query", {"range": "day"}).result
        second = client.call("stats.query", {"range": "day"}).result

        assert first["first_day"] == second["first_day"]


class TestDeleting:
    def test_one_request_empties_everything(
        self, client: EngineClient, engine: Engine, clock: FakeClock
    ) -> None:
        a_finished_session(engine, clock)

        response = client.call("stats.delete")

        assert response.ok
        assert client.call("stats.query", {"range": "month"}).result["blocked_attempts"] == 0

    def test_it_works_during_a_session(self, client: EngineClient, engine: Engine) -> None:
        """A person's own history is not part of what a session enforces."""
        client.call(
            "session.start",
            {"profile": "Study", "duration_seconds": HOUR, "level": "strict", "valve": "wait"},
        )

        assert client.call("stats.delete").ok


class TestTheCommandLine:
    def test_it_prints_the_headline_numbers(
        self, client: EngineClient, engine: Engine, clock: FakeClock, paths: Paths
    ) -> None:
        a_finished_session(engine, clock)

        code, out, err = cli(paths, "stats", "--day")

        assert code == 0, err
        assert "Focus" in out
        assert "Sessions completed   1" in out

    def test_it_ranks_what_was_blocked(
        self, client: EngineClient, engine: Engine, clock: FakeClock, paths: Paths
    ) -> None:
        a_finished_session(engine, clock)

        _code, out, _err = cli(paths, "stats", "--week")

        assert "youtube.com" in out

    def test_an_application_is_marked_as_one(
        self, client: EngineClient, engine: Engine, clock: FakeClock, paths: Paths
    ) -> None:
        """The mockup writes "Discord (app)" beside the domains."""
        from anchor.protocol.messages import Request

        engine.handle(
            Request(
                type="session.start",
                payload={
                    "profile": "Study",
                    "duration_seconds": HOUR,
                    "level": "soft",
                    "origin": "manual",
                    "valve": None,
                },
            )
        )
        engine.handle(
            Request(
                type="apps.report",
                payload={"kind": "launch", "apps": ["Discord"], "seconds": 0},
            )
        )

        _code, out, _err = cli(paths, "stats", "--day")

        assert "Discord (app)" in out

    def test_json_is_available_like_everywhere_else(
        self, client: EngineClient, paths: Paths
    ) -> None:
        import json

        code, out, _err = cli(paths, "stats", "--json")

        assert code == 0
        assert json.loads(out)["result"]["view"] == "week"

    def test_deleting_asks_first(
        self, client: EngineClient, engine: Engine, clock: FakeClock, paths: Paths
    ) -> None:
        """It cannot be undone, so it is not a thing you do by mistyping."""
        a_finished_session(engine, clock)

        code, _out, err = cli(paths, "stats", "--delete", stdin="no\n")

        assert code == 2
        assert "nothing was deleted" in err
        assert client.call("stats.query", {"range": "week"}).result["blocked_attempts"] == 1

    def test_and_deletes_when_told_to(
        self, client: EngineClient, engine: Engine, clock: FakeClock, paths: Paths
    ) -> None:
        a_finished_session(engine, clock)

        code, out, err = cli(paths, "stats", "--delete", stdin="delete\n")

        assert code == 0, err
        assert "deleted" in out
        assert client.call("stats.query", {"range": "week"}).result["blocked_attempts"] == 0
