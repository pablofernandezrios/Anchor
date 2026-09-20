"""Application blocking end to end (SPEC 7.1, 9).

A real engine over a real socket, the real blocker daemon polling it, the real
process watcher, and a real process that gets closed. Everything between the
profile and the dead process is exercised; only the firewall commands are
recorded rather than run, because a test must not reconfigure the machine it
runs on.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from anchor.blocker.apps import AppKind, InstalledApp
from anchor.blocker.daemon import BlockerDaemon, Lists
from anchor.blocker.enforcement import AppEnforcer, is_alive
from anchor.blocker.watcher import wait_for
from anchor.cli.client import EngineClient
from anchor.engine.core import Engine
from anchor.engine.paths import Paths, Settings
from anchor.engine.profiles import Profile
from anchor.engine.service import EngineServer
from anchor.engine.timekeeping import FakeClock
from anchor.protocol.messages import Event
from anchor.protocol.types import WebMode
from anchor.system.commands import RecordingRunner, Result

HOUR = 3600
APP_ID = "test-app.desktop"

STATUS = """\
Link 2 (eth0)
       DNS Servers: 192.168.1.1
"""


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    tree = Paths.resolve(tmp_path)
    tree.ensure_directories()
    return tree


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(wall_time=1_760_000_000.0, boottime=1_000.0, boot="boot-a")


@pytest.fixture
def events() -> list[Event]:
    return []


@pytest.fixture
def engine(paths: Paths, clock: FakeClock, tmp_path: Path, events: list[Event]) -> Engine:
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
        apps=frozenset({APP_ID}),
    )
    engine.save_config()
    engine.subscribe(events.append)
    return engine


@pytest.fixture
def served(engine: Engine, paths: Paths) -> Iterator[Engine]:
    """The engine listening on its socket, as the blocker expects to find it."""
    server = EngineServer(engine)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield engine
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def binary(tmp_path: Path) -> Path:
    source = shutil.which("sleep")
    assert source, "this test needs /usr/bin/sleep"
    target = tmp_path / "anchor-test-app"
    shutil.copy(source, target)
    return target


@pytest.fixture
def app(binary: Path) -> InstalledApp:
    return InstalledApp(id=APP_ID, name="Test App", kind=AppKind.NATIVE, exec_path=str(binary))


@pytest.fixture
def daemon(paths: Paths, app: InstalledApp, tmp_path: Path) -> Iterator[BlockerDaemon]:
    data = Path(__file__).resolve().parents[2] / "data"
    daemon = BlockerDaemon(
        paths,
        lists=Lists.load(data),
        runner=RecordingRunner(
            {
                "resolvectl status": Result(code=0, out=STATUS),
                "is-active": Result(code=0, out="active"),
            }
        ),
        resolver_port=5399,
        policy_root=tmp_path / "policies",
        resolved_drop_in=tmp_path / "run" / "systemd" / "resolved.conf.d" / "50-anchor.conf",
    )
    # The catalogue is the one thing that cannot be real here: discovery would
    # find this machine's applications, and the test would close them.
    daemon.enforcer = AppEnforcer(
        catalogue=lambda: [app],
        on_grace=daemon.report_grace,
        on_closed=daemon.report_closed,
        kill_after=5.0,
    )
    try:
        yield daemon
    finally:
        daemon.stop_enforcing()


@pytest.fixture
def running(binary: Path) -> Iterator[list[subprocess.Popen[bytes]]]:
    started: list[subprocess.Popen[bytes]] = []
    yield started
    for child in started:
        if child.poll() is None:
            child.kill()
        child.wait()


def launch(binary: Path, started: list[subprocess.Popen[bytes]]) -> subprocess.Popen[bytes]:
    child = subprocess.Popen([str(binary), "60"])
    started.append(child)
    assert wait_for(lambda: is_alive(child.pid), timeout=5)
    return child


def start_session(paths: Paths) -> None:
    """Start it the way a person would: over the socket."""
    with EngineClient(paths.engine_socket, timeout=5.0) as client:
        response = client.call(
            "session.start",
            {"profile": "Study", "duration_seconds": HOUR, "level": "firm"},
        )
    assert response.ok, response.error


def names(events: list[Event], kind: str) -> list[list[str]]:
    return [event.payload["apps"] for event in events if event.event == kind]


class TestFromProfileToDeadProcess:
    def test_the_application_survives_the_grace_and_then_does_not(
        self,
        served: Engine,
        paths: Paths,
        daemon: BlockerDaemon,
        binary: Path,
        running: list[subprocess.Popen[bytes]],
        clock: FakeClock,
        events: list[Event],
    ) -> None:
        child = launch(binary, running)
        start_session(paths)

        daemon.poll()

        assert child.poll() is None, "it was closed before its two minutes were up"
        assert names(events, "apps.grace") == [["Test App"]]

        clock.advance(121)
        daemon.poll()

        assert child.wait(timeout=10) == -signal.SIGTERM
        assert wait_for(lambda: bool(names(events, "apps.closed")), timeout=5)
        assert names(events, "apps.closed") == [["Test App"]]

    def test_the_engine_counts_it(
        self,
        served: Engine,
        paths: Paths,
        daemon: BlockerDaemon,
        binary: Path,
        running: list[subprocess.Popen[bytes]],
        clock: FakeClock,
    ) -> None:
        """SPEC 13: blocked attempts by application."""
        child = launch(binary, running)
        start_session(paths)
        clock.advance(121)

        daemon.poll()
        child.wait(timeout=10)

        assert wait_for(lambda: served.status()["app_blocks"] == 1, timeout=5)

    def test_launching_it_again_closes_it_at_once(
        self,
        served: Engine,
        paths: Paths,
        daemon: BlockerDaemon,
        binary: Path,
        running: list[subprocess.Popen[bytes]],
        clock: FakeClock,
    ) -> None:
        """SPEC 9: killed immediately on launch, by the watcher this time."""
        start_session(paths)
        clock.advance(121)
        daemon.poll()

        child = launch(binary, running)

        assert child.wait(timeout=10) == -signal.SIGTERM

    def test_ending_the_session_lets_it_run_again(
        self,
        served: Engine,
        paths: Paths,
        daemon: BlockerDaemon,
        binary: Path,
        running: list[subprocess.Popen[bytes]],
        clock: FakeClock,
    ) -> None:
        """A session that has ended must not keep killing things (P4)."""
        start_session(paths)
        clock.advance(121)
        daemon.poll()

        served.state.session = None
        served.save()
        daemon.poll()

        child = launch(binary, running)
        time.sleep(0.5)

        assert child.poll() is None
        assert not daemon.enforcer.enforcing
