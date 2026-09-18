"""The engine over a real socket: authorisation and the session lifecycle.

These exercise the parts that unit tests cannot: ``SO_PEERCRED``, the line
framing, and the state surviving a restart of the engine.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from anchor.cli.client import EngineClient
from anchor.engine.core import Engine
from anchor.engine.paths import Paths, Settings
from anchor.engine.service import EngineServer
from anchor.engine.sessions import SessionPolicy
from anchor.engine.timekeeping import FakeClock
from anchor.protocol.errors import ErrorCode

HOUR = 3600


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    tree = Paths.resolve(tmp_path)
    tree.ensure_directories()
    return tree


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(wall_time=1_760_000_000.0, boottime=1_000.0, boot="boot-a")


@pytest.fixture
def engine(paths: Paths, clock: FakeClock) -> Engine:
    engine = Engine(
        paths,
        Settings(owner_uid=os.getuid()),
        clock=clock,
        policy=SessionPolicy(),
    )
    engine.load()
    return engine


@pytest.fixture
def server(engine: Engine) -> Iterator[EngineServer]:
    server = EngineServer(engine)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def client(server: EngineServer, paths: Paths) -> Iterator[EngineClient]:
    with EngineClient(paths.engine_socket, timeout=5.0) as client:
        yield client


def test_status_on_an_idle_engine(client: EngineClient) -> None:
    response = client.call("status.get")
    assert response.ok
    assert response.result["active"] is False
    assert response.result["skips_remaining"] == 3


def test_a_session_runs_through_its_whole_life(client: EngineClient, clock: FakeClock) -> None:
    started = client.call(
        "session.start",
        {"profile": "Study", "duration_seconds": 2 * HOUR, "level": "soft"},
    )
    assert started.ok
    assert started.result["active"] is True
    assert started.result["remaining_seconds"] == pytest.approx(2 * HOUR)

    clock.advance(30 * 60)
    assert client.call("status.get").result["remaining_seconds"] == pytest.approx(1.5 * HOUR)

    extended = client.call("session.extend", {"by_seconds": 30 * 60})
    assert extended.result["remaining_seconds"] == pytest.approx(2 * HOUR)

    # Cancelling a Soft session costs a five-minute wait and nothing else.
    pending = client.call("session.cancel")
    assert pending.result["exit_request"]["remaining_wait_seconds"] == pytest.approx(300)

    clock.advance(301)
    assert client.call("status.get").result["active"] is False


def test_starting_twice_is_refused(client: EngineClient) -> None:
    payload = {"profile": "Study", "duration_seconds": HOUR, "level": "soft"}
    assert client.call("session.start", payload).ok

    second = client.call("session.start", payload)
    assert not second.ok
    assert second.code == ErrorCode.SESSION_ALREADY_ACTIVE


def test_a_manual_session_cannot_exceed_eight_hours(client: EngineClient) -> None:
    response = client.call(
        "session.start",
        {"profile": "Study", "duration_seconds": 9 * HOUR, "level": "soft"},
    )
    assert not response.ok
    assert response.code == ErrorCode.DURATION_TOO_LONG


def test_a_strict_session_refuses_to_be_cancelled(client: EngineClient) -> None:
    client.call(
        "session.start",
        {
            "profile": "Study",
            "duration_seconds": 2 * HOUR,
            "level": "strict",
            "valve": "wait",
        },
    )
    refused = client.call("session.cancel")
    assert not refused.ok
    assert refused.code == ErrorCode.CANCEL_FORBIDDEN


def test_the_valve_lets_go_after_its_wait_and_records_a_rupture(
    client: EngineClient, clock: FakeClock, engine: Engine
) -> None:
    client.call(
        "session.start",
        {
            "profile": "Study",
            "duration_seconds": 4 * HOUR,
            "level": "strict",
            "valve": "wait",
        },
    )
    requested = client.call("valve.request")
    assert requested.result["exit_request"]["remaining_wait_seconds"] == pytest.approx(30 * 60)

    clock.advance(10 * 60)
    assert client.call("status.get").result["active"] is True

    clock.advance(21 * 60)
    assert client.call("status.get").result["active"] is False
    assert [rupture["kind"] for rupture in engine.state.ruptures] == ["valve"]


def test_a_valve_request_can_be_withdrawn(client: EngineClient, clock: FakeClock) -> None:
    client.call(
        "session.start",
        {
            "profile": "Study",
            "duration_seconds": 4 * HOUR,
            "level": "strict",
            "valve": "wait",
        },
    )
    client.call("valve.request")
    assert client.call("valve.withdraw").result["exit_request"] is None

    clock.advance(31 * 60)
    assert client.call("status.get").result["active"] is True


def test_the_phrase_valve_needs_the_right_phrase(client: EngineClient, engine: Engine) -> None:
    client.call(
        "session.start",
        {
            "profile": "Study",
            "duration_seconds": 4 * HOUR,
            "level": "strict",
            "valve": "phrase",
        },
    )
    requested = client.call("valve.request")
    phrase = requested.result["exit_request"]["phrase"]
    assert phrase and len(phrase) >= 150

    wrong = client.call("valve.phrase", {"text": "let me out"})
    assert not wrong.ok
    assert wrong.code == ErrorCode.PHRASE_MISMATCH
    assert client.call("status.get").result["active"] is True

    right = client.call("valve.phrase", {"text": phrase})
    assert right.ok
    assert right.result["ended"] is True


def test_unknown_fields_are_rejected(client: EngineClient) -> None:
    response = client.call(
        "session.start",
        {
            "profile": "Study",
            "duration_seconds": HOUR,
            "level": "soft",
            "sneaky": True,
        },
    )
    assert not response.ok
    assert response.code == ErrorCode.BAD_REQUEST


def test_an_unknown_request_type_is_rejected(client: EngineClient) -> None:
    response = client.call("session.obliterate")
    assert not response.ok
    assert response.code == ErrorCode.UNKNOWN_TYPE


def test_a_registered_but_unbuilt_command_says_so(client: EngineClient) -> None:
    response = client.call("doctor.run")
    assert not response.ok
    assert response.code == ErrorCode.NOT_IMPLEMENTED


def test_a_stranger_is_turned_away(server: EngineServer) -> None:
    """Only root and the owner may give orders (SPEC 5.2)."""
    assert server.is_authorised(0)
    assert server.is_authorised(os.getuid())
    assert not server.is_authorised(os.getuid() + 1)
    assert not server.is_authorised(65534)


def test_a_session_survives_the_engine_restarting(
    client: EngineClient, paths: Paths, clock: FakeClock
) -> None:
    """A package upgrade restarts the daemons mid-session (SPEC 17)."""
    client.call(
        "session.start",
        {"profile": "Study", "duration_seconds": 2 * HOUR, "level": "firm"},
    )

    clock.advance(45 * 60)
    revived = Engine(paths, Settings(owner_uid=os.getuid()), clock=clock)
    revived.load()

    status = revived.status()
    assert status["active"] is True
    assert status["level"] == "firm"
    assert status["remaining_seconds"] == pytest.approx(75 * 60)


def test_editing_state_by_hand_is_a_rupture_and_does_not_free_the_session(
    client: EngineClient, paths: Paths, clock: FakeClock
) -> None:
    """SPEC 6.1: a mismatch is a rupture, and the stricter reading is kept."""
    client.call(
        "session.start",
        {
            "profile": "Study",
            "duration_seconds": 4 * HOUR,
            "level": "strict",
            "valve": "phrase",
        },
    )

    # Someone edits state.json to shorten the session. The signature no longer
    # matches, so the engine notices.
    raw = paths.state_file.read_text(encoding="utf-8")
    tampered = raw.replace('"duration_seconds":14400', '"duration_seconds":60')
    assert tampered != raw
    paths.state_file.write_text(tampered, encoding="utf-8")

    revived = Engine(paths, Settings(owner_uid=os.getuid()), clock=clock)
    revived.load()

    assert revived.status()["active"] is True
    assert [rupture["kind"] for rupture in revived.state.ruptures] == ["tampering"]
