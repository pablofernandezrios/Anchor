"""The interface talking to a real engine over its socket (SPEC 5.1)."""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from anchor.blocker.watcher import wait_for
from anchor.engine.core import Engine
from anchor.engine.paths import Paths, Settings
from anchor.engine.profiles import Profile
from anchor.engine.service import EngineServer
from anchor.engine.timekeeping import FakeClock
from anchor.gui.engine import EngineLink, Reply


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    tree = Paths.resolve(tmp_path)
    tree.ensure_directories()
    return tree


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(wall_time=1_760_000_000.0, boottime=1_000.0, boot="boot-a")


@pytest.fixture
def engine(paths: Paths, clock: FakeClock, tmp_path: Path) -> Engine:
    built = Engine(
        paths,
        Settings(owner_uid=os.getuid()),
        clock=clock,
        systemd_runtime_dir=tmp_path / "run" / "systemd" / "system",
    )
    built.load()
    built.profiles["Study"] = Profile(name="Study", domains=frozenset({"youtube.com"}))
    built.save_config()
    return built


@pytest.fixture
def server(engine: Engine) -> Iterator[EngineServer]:
    running = EngineServer(engine)
    thread = threading.Thread(target=running.serve_forever, daemon=True)
    thread.start()
    try:
        yield running
    finally:
        running.shutdown()
        thread.join(timeout=5)


@pytest.fixture
def link(server: EngineServer, paths: Paths) -> Iterator[EngineLink]:
    built = EngineLink(paths.engine_socket, timeout=5.0)
    try:
        yield built
    finally:
        built.stop()


class TestAsking:
    def test_an_answer_comes_back(self, link: EngineLink) -> None:
        reply = link.ask_now("status.get")

        assert reply.ok
        assert reply.result["active"] is False

    def test_a_refusal_is_an_answer_with_a_sentence_in_it(self, link: EngineLink) -> None:
        reply = link.ask_now("session.extend", {"by_seconds": 60})

        assert not reply.ok
        assert reply.refused
        assert reply.code == "NO_ACTIVE_SESSION"
        assert reply.message

    def test_an_engine_that_is_not_there_is_not_a_refusal(self, tmp_path: Path) -> None:
        """A broken machine and a rule saying no are different screens."""
        nowhere = EngineLink(tmp_path / "nothing.sock", timeout=1.0)
        reply = nowhere.ask_now("status.get")

        assert not reply.ok
        assert not reply.refused
        assert reply.code == "UNREACHABLE"

    def test_a_request_from_a_click_answers_on_the_drawing_thread(
        self, server: EngineServer, paths: Paths
    ) -> None:
        drawn: list[str] = []
        answers: list[Reply] = []

        def here(work: Any) -> None:
            drawn.append(threading.current_thread().name)
            work()

        link = EngineLink(paths.engine_socket, schedule=here, timeout=5.0)

        link.ask("status.get", None, answers.append)

        assert wait_for(lambda: bool(answers), timeout=5)
        assert answers[0].ok
        # Handed back through the scheduler, not called on the socket thread.
        assert drawn


class TestAskingForSeveralThingsAtOnce:
    def test_one_answer_carries_all_the_results(self, link: EngineLink) -> None:
        answers: list[Reply] = []

        link.ask_all([("status.get", {}), ("profile.list", {})], answers.append)

        assert wait_for(lambda: bool(answers), timeout=5)
        reply = answers[0]
        assert reply.ok
        assert reply.result["status.get"]["active"] is False
        assert reply.result["profile.list"]["profiles"]

    def test_one_refusal_does_not_lose_the_rest(self, link: EngineLink) -> None:
        answers: list[Reply] = []

        link.ask_all(
            [("status.get", {}), ("profile.show", {"name": "Nope"})],
            answers.append,
        )

        assert wait_for(lambda: bool(answers), timeout=5)
        reply = answers[0]
        assert not reply.ok
        assert reply.result["status.get"]["active"] is False
        assert "Nope" in reply.result["profile.show.error"]

    def test_an_outage_answers_once_and_says_so(self, tmp_path: Path) -> None:
        nowhere = EngineLink(tmp_path / "nothing.sock", timeout=1.0)
        answers: list[Reply] = []

        nowhere.ask_all([("status.get", {})], answers.append)

        assert wait_for(lambda: bool(answers), timeout=5)
        assert answers[0].code == "UNREACHABLE"

    def test_an_outage_survives_a_scheduler_that_runs_later(self, tmp_path: Path) -> None:
        """Which GTK's does: idle_add runs the work on the next idle.

        A scheduler that runs its callback immediately hides a whole class of
        mistake here, because Python unbinds an `except ... as` name at the
        end of the block. This is the only scheduler shape the real one has.
        """
        queued: list[Any] = []
        nowhere = EngineLink(tmp_path / "nothing.sock", schedule=queued.append, timeout=1.0)
        answers: list[Reply] = []

        nowhere.ask_all([("status.get", {})], answers.append)
        assert wait_for(lambda: bool(queued), timeout=5)
        for work in list(queued):
            work()

        assert answers[0].code == "UNREACHABLE"


class TestFollowingTheEngine:
    def test_the_status_arrives_without_being_asked(self, link: EngineLink) -> None:
        seen: list[dict[str, Any]] = []
        link.watch_status(seen.append)
        link.start()

        assert wait_for(lambda: bool(seen), timeout=5)
        assert seen[0]["active"] is False
        assert link.connected

    def test_losing_the_engine_is_announced(self, server: EngineServer, paths: Paths) -> None:
        """So the window can say the time left is unknown rather than invent it."""
        link = EngineLink(paths.engine_socket, timeout=2.0)
        connections: list[bool] = []
        link.watch_connection(connections.append)
        link.start()

        assert wait_for(lambda: connections == [True], timeout=5)
        server.shutdown()

        assert wait_for(lambda: connections[-1] is False, timeout=10)
        link.stop()

    def test_a_listener_that_raises_does_not_end_the_feed(self, link: EngineLink) -> None:
        seen: list[dict[str, Any]] = []

        def bad(_status: dict[str, Any]) -> None:
            raise RuntimeError("this screen is broken")

        link.watch_status(bad)
        link.watch_status(seen.append)
        link.start()

        assert wait_for(lambda: bool(seen), timeout=5)
