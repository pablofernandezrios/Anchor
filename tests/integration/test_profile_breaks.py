"""Editing a profile's break settings (SPEC 12, 10, mockup 4).

SPEC 12 makes the break pattern part of a profile and the mockup draws it as
something the user picks. The protocol carried no way to say so until the
interface needed one.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from anchor.engine.core import Engine
from anchor.engine.paths import Paths, Settings
from anchor.engine.profiles import BreakSettings, Profile
from anchor.engine.timekeeping import FakeClock
from anchor.protocol.errors import ErrorCode, ProtocolError
from anchor.protocol.messages import Request, Response, new_id
from anchor.protocol.types import BreakHardness, BreakType


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(wall_time=1_760_000_000.0, boottime=1_000.0, boot="boot-a")


@pytest.fixture
def engine(tmp_path: Path, clock: FakeClock) -> Engine:
    paths = Paths.resolve(tmp_path)
    paths.ensure_directories()
    built = Engine(
        paths,
        Settings(owner_uid=os.getuid()),
        clock=clock,
        systemd_runtime_dir=tmp_path / "run" / "systemd" / "system",
    )
    built.load()
    built.profiles["Study"] = Profile(name="Study", breaks=BreakSettings())
    built.save_config()
    return built


def ask(engine: Engine, type_: str, **payload: Any) -> Any:
    """Send a request the way the socket does, malformed ones included.

    The server turns a schema refusal into a failed response rather than an
    exception, so a test that called ``Engine.handle`` directly would never
    see the answer a client gets.
    """
    try:
        request = Request.from_dict({"v": 1, "id": new_id(), "type": type_, "payload": payload})
    except ProtocolError as error:
        return Response(id="malformed", ok=False, error=error.as_payload())
    return engine.handle(request)


def breaks(engine: Engine) -> BreakSettings:
    return engine.profiles["Study"].breaks


class TestChangingThePattern:
    def test_the_pattern_can_be_changed(self, engine: Engine) -> None:
        response = ask(engine, "profile.edit", name="Study", breaks={"work_minutes": 25})

        assert response.ok, response.error
        assert breaks(engine).work_minutes == 25

    def test_what_is_not_mentioned_is_left_alone(self, engine: Engine) -> None:
        before = breaks(engine).break_minutes
        ask(engine, "profile.edit", name="Study", breaks={"work_minutes": 90})

        assert breaks(engine).break_minutes == before

    def test_the_type_and_the_hardness_can_be_chosen(self, engine: Engine) -> None:
        ask(
            engine,
            "profile.edit",
            name="Study",
            breaks={"type": "notification", "hardness": "flexible"},
        )

        assert breaks(engine).type is BreakType.NOTIFICATION
        assert breaks(engine).hardness is BreakHardness.FLEXIBLE

    def test_a_long_break_can_be_added(self, engine: Engine) -> None:
        ask(
            engine,
            "profile.edit",
            name="Study",
            breaks={"long_break_every": 4, "long_break_minutes": 20},
        )

        assert breaks(engine).long_break_every == 4
        assert breaks(engine).long_break_minutes == 20

    def test_and_turned_off_again_with_zero(self, engine: Engine) -> None:
        """Absent already means "unchanged", so it cannot also mean "remove"."""
        ask(
            engine,
            "profile.edit",
            name="Study",
            breaks={"long_break_every": 4, "long_break_minutes": 20},
        )
        ask(engine, "profile.edit", name="Study", breaks={"long_break_every": 0})

        assert breaks(engine).long_break_every is None
        assert breaks(engine).long_break_minutes is None

    def test_a_pattern_that_makes_no_sense_is_refused(self, engine: Engine) -> None:
        response = ask(engine, "profile.edit", name="Study", breaks={"work_minutes": 0})

        assert not response.ok
        assert response.code == ErrorCode.BAD_REQUEST

    def test_a_key_nobody_recognises_is_refused(self, engine: Engine) -> None:
        """SPEC 5.2: unknown fields are rejected, nested ones included."""
        response = ask(engine, "profile.edit", name="Study", breaks={"colour": 1})

        assert not response.ok
        assert response.code == ErrorCode.BAD_REQUEST

    def test_a_new_profile_can_carry_a_pattern(self, engine: Engine) -> None:
        response = ask(
            engine,
            "profile.create",
            name="Reading",
            breaks={"work_minutes": 90, "break_minutes": 20},
        )

        assert response.ok, response.error
        assert engine.profiles["Reading"].breaks.work_minutes == 90


class TestDuringASession:
    def start(self, engine: Engine) -> None:
        response = ask(
            engine, "session.start", profile="Study", duration_seconds=3600, level="firm"
        )
        assert response.ok, response.error

    def test_the_pattern_still_changes(self, engine: Engine) -> None:
        """The ratchet is about what is blocked, and this is not that."""
        self.start(engine)
        response = ask(engine, "profile.edit", name="Study", breaks={"work_minutes": 25})

        assert response.ok, response.error

    def test_but_letting_sites_through_on_breaks_does_not(self, engine: Engine) -> None:
        self.start(engine)
        response = ask(
            engine,
            "profile.edit",
            name="Study",
            breaks={"allow_sites_during_breaks": True},
        )

        assert not response.ok
        assert response.code == ErrorCode.RATCHET_VIOLATION
