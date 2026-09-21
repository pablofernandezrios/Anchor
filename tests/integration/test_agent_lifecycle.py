"""``anchor-agent`` as systemd runs it: follow an engine, then stop (SPEC 5.1).

Two separate processes and a real socket between them. The agent runs in
``--dry-run``, which does everything except touch the session bus: this
machine has no desktop, and the part that draws is checked on one that does
(``tools/check_agent.py``).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from anchor.cli.client import EngineClient
from anchor.engine.paths import Paths

# A relocated tree still has to fit the kernel's limit for Unix socket paths,
# which pytest's own temporary directories comfortably exceed.
SHORT_ROOT = Path("/tmp/anchor-tests")  # noqa: S108


def _wait_for(path: Path, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    raise AssertionError(f"{path} did not appear within {timeout} seconds")


class Reader:
    """Reads a process's output without blocking the test on it."""

    def __init__(self, process: subprocess.Popen[str]) -> None:
        self.process = process
        self.lines: list[str] = []
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def _read(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self.lines.append(line.rstrip("\n"))

    def saw(self, text: str, *, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if any(text in line for line in list(self.lines)):
                return True
            time.sleep(0.05)
        return False

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


@pytest.fixture
def engine() -> Iterator[Paths]:
    root = SHORT_ROOT / f"agent-{os.getpid()}"
    if root.exists():
        subprocess.run(["rm", "-rf", str(root)], check=True)
    root.mkdir(parents=True)

    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "anchor.engine.main",
            "--root",
            str(root),
            "--owner-uid",
            str(os.getuid()),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    paths = Paths.resolve(root)
    try:
        _wait_for(paths.engine_socket)
        yield paths
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover
                process.kill()
        subprocess.run(["rm", "-rf", str(root)], check=False)


@pytest.fixture
def agent(engine: Paths) -> Iterator[tuple[subprocess.Popen[str], Reader, Paths]]:
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "anchor.agent.main",
            "--root",
            str(engine.state_dir.parents[2]),
            "--dry-run",
            "--verbose",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    reader = Reader(process)
    try:
        yield process, reader, engine
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def start_session(paths: Paths) -> None:
    with EngineClient(paths.engine_socket, timeout=5.0) as client:
        response = client.call(
            "session.start",
            {"profile": "Study", "duration_seconds": 7200, "level": "firm"},
        )
        assert response.ok, response.error


class TestFollowingAlong:
    def test_it_connects_to_the_engine(
        self, agent: tuple[subprocess.Popen[str], Reader, Paths]
    ) -> None:
        _, reader, _ = agent

        assert reader.saw("connected to the engine"), reader.text

    def test_a_session_reaches_the_indicator(
        self, agent: tuple[subprocess.Popen[str], Reader, Paths]
    ) -> None:
        _, reader, paths = agent
        assert reader.saw("connected to the engine")

        start_session(paths)

        assert reader.saw("visible=True"), reader.text

    def test_a_blocked_site_becomes_a_notification(
        self, agent: tuple[subprocess.Popen[str], Reader, Paths]
    ) -> None:
        """The whole chain: blocker's report, engine's event, agent's words."""
        _, reader, paths = agent
        assert reader.saw("connected to the engine")
        start_session(paths)

        with EngineClient(paths.engine_socket, timeout=5.0) as client:
            reported = client.call(
                "blocked.report", {"domain": "youtube.com", "rule": "youtube.com"}
            )
            assert reported.ok, reported.error

        assert reader.saw("notification: Site blocked"), reader.text
        assert reader.saw("youtube.com is blocked during this session"), reader.text

    def test_closing_an_application_becomes_a_notification(
        self, agent: tuple[subprocess.Popen[str], Reader, Paths]
    ) -> None:
        _, reader, paths = agent
        assert reader.saw("connected to the engine")
        start_session(paths)

        with EngineClient(paths.engine_socket, timeout=5.0) as client:
            client.call("apps.report", {"kind": "launch", "apps": ["Discord"]})

        assert reader.saw("notification: Discord is blocked"), reader.text


class TestStopping:
    def test_it_stops_promptly_on_sigterm(
        self, agent: tuple[subprocess.Popen[str], Reader, Paths]
    ) -> None:
        """systemd restarts this on every login; a slow stop is a slow login."""
        process, reader, _ = agent
        assert reader.saw("connected to the engine")

        process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            process.kill()
            pytest.fail("the agent did not exit within 10 seconds of SIGTERM")

        assert process.returncode == 0


class TestWhenTheEngineIsNotThereYet:
    def test_the_agent_waits_instead_of_exiting(self, tmp_path: Path) -> None:
        """The user session starts before the system one has finished."""
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "anchor.agent.main",
                "--root",
                str(tmp_path),
                "--dry-run",
                "--verbose",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        reader = Reader(process)
        try:
            assert reader.saw("not reachable"), reader.text
            time.sleep(0.5)
            assert process.poll() is None, "the agent gave up"
        finally:
            process.send_signal(signal.SIGTERM)
            process.wait(timeout=10)
