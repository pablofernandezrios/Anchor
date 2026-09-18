"""The daemon as systemd runs it: start, serve, and stop on SIGTERM."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from anchor.cli.client import EngineClient
from anchor.engine.paths import Paths

# A relocated tree still has to fit inside the kernel's limit for Unix socket
# paths, which pytest's own temporary directories comfortably exceed.
SHORT_ROOT = Path("/tmp/anchor-tests")  # noqa: S108


def _wait_for(path: Path, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    raise AssertionError(f"{path} did not appear within {timeout} seconds")


@pytest.fixture
def daemon() -> object:
    root = SHORT_ROOT / f"run-{os.getpid()}"
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
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    paths = Paths.resolve(root)
    try:
        _wait_for(paths.engine_socket)
        yield process, paths
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        subprocess.run(["rm", "-rf", str(root)], check=False)


def test_the_daemon_answers_over_its_socket(daemon: tuple[subprocess.Popen[str], Paths]) -> None:
    _, paths = daemon
    with EngineClient(paths.engine_socket, timeout=5.0) as client:
        assert client.call("status.get").result["active"] is False


def test_the_daemon_stops_promptly_on_sigterm(
    daemon: tuple[subprocess.Popen[str], Paths],
) -> None:
    """A stop must not wait for systemd to lose patience and send SIGKILL.

    ``socketserver.shutdown()`` blocks until ``serve_forever()`` returns, so
    calling it from a signal handler on that same thread deadlocks. This is the
    regression guard for that.
    """
    process, paths = daemon

    process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:  # pragma: no cover - the bug being guarded
        process.kill()
        pytest.fail("the daemon did not exit within 10 seconds of SIGTERM")

    assert process.returncode == 0
    assert not paths.engine_socket.exists(), "the socket was left behind"


def test_state_written_by_one_daemon_is_read_by_the_next(
    daemon: tuple[subprocess.Popen[str], Paths],
) -> None:
    process, paths = daemon
    with EngineClient(paths.engine_socket, timeout=5.0) as client:
        started = client.call(
            "session.start",
            {"profile": "Study", "duration_seconds": 7200, "level": "firm"},
        )
        assert started.ok
        session_id = started.result["session_id"]

    process.send_signal(signal.SIGTERM)
    process.wait(timeout=10)

    stored = json.loads(paths.state_file.read_text(encoding="utf-8"))
    assert stored["data"]["session"]["id"] == session_id

    revived = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "anchor.engine.main",
            "--root",
            str(paths.state_dir.parents[2]),
            "--owner-uid",
            str(os.getuid()),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait_for(paths.engine_socket)
        with EngineClient(paths.engine_socket, timeout=5.0) as client:
            status = client.call("status.get").result
        assert status["active"] is True
        assert status["session_id"] == session_id
    finally:
        revived.send_signal(signal.SIGTERM)
        revived.wait(timeout=10)
