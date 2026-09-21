"""The engine answering config.get and config.set (SPEC 7.2, 13, 15)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from anchor.engine.core import Engine
from anchor.engine.paths import Paths, Settings
from anchor.engine.profiles import Profile
from anchor.engine.sessions import DEFAULT_FIRM_WAIT_SECONDS
from anchor.engine.timekeeping import FakeClock
from anchor.protocol.errors import ErrorCode
from anchor.protocol.messages import Request, new_id
from anchor.protocol.types import WebMode


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(wall_time=1_760_000_000.0, boottime=1_000.0, boot="boot-a")


def build(tmp_path: Path, clock: FakeClock, *, retention: int = 90) -> Engine:
    paths = Paths.resolve(tmp_path)
    paths.ensure_directories()
    engine = Engine(
        paths,
        Settings(owner_uid=os.getuid(), retention_days=retention),
        clock=clock,
        systemd_runtime_dir=tmp_path / "run" / "systemd" / "system",
    )
    engine.load()
    engine.profiles["Study"] = Profile(name="Study", web_mode=WebMode.BLOCKLIST)
    engine.save_config()
    return engine


@pytest.fixture
def engine(tmp_path: Path, clock: FakeClock) -> Engine:
    return build(tmp_path, clock)


def ask(engine: Engine, type_: str, **payload: Any) -> Any:
    """Send a request the way the socket would, defaults and all."""
    request = Request.from_dict({"v": 1, "id": new_id(), "type": type_, "payload": payload})
    return engine.handle(request)


def setting(engine: Engine, key: str) -> dict[str, Any]:
    response = ask(engine, "config.get", key=key)
    assert response.ok, response.error
    entry: dict[str, Any] = response.result["settings"][0]
    return entry


def start(engine: Engine, level: str = "firm") -> None:
    payload: dict[str, Any] = {"profile": "Study", "duration_seconds": 3600, "level": level}
    if level == "strict":
        payload["valve"] = "phrase"
    response = ask(engine, "session.start", **payload)
    assert response.ok, response.error


class TestReading:
    def test_every_setting_comes_back_at_once(self, engine: Engine) -> None:
        response = ask(engine, "config.get")
        assert response.ok
        keys = {entry["key"] for entry in response.result["settings"]}
        assert keys == {
            "firm_wait_seconds",
            "phrase_length",
            "retention_days",
            "language",
            "onboarding_done",
        }

    def test_one_setting_comes_back_alone(self, engine: Engine) -> None:
        entry = setting(engine, "firm_wait_seconds")
        assert entry["value"] == DEFAULT_FIRM_WAIT_SECONDS
        assert entry["is_default"] is True
        assert entry["during_session"] is False
        assert entry["explain"]

    def test_asking_for_a_setting_that_does_not_exist_is_refused(self, engine: Engine) -> None:
        response = ask(engine, "config.get", key="colour")
        assert not response.ok
        assert response.code == ErrorCode.INVALID_CONFIG

    def test_the_installed_retention_is_the_default_until_someone_chooses(
        self, tmp_path: Path, clock: FakeClock
    ) -> None:
        engine = build(tmp_path, clock, retention=45)
        entry = setting(engine, "retention_days")
        assert (entry["value"], entry["is_default"]) == (45, True)


class TestWriting:
    def test_a_change_is_acknowledged_with_the_value_it_took(self, engine: Engine) -> None:
        response = ask(engine, "config.set", key="firm_wait_seconds", value="1800")
        assert response.ok
        assert response.result == {"key": "firm_wait_seconds", "value": 1800}

    def test_a_change_reaches_the_next_session_s_wait(self, engine: Engine) -> None:
        ask(engine, "config.set", key="firm_wait_seconds", value="1800")
        assert engine.policy.firm_wait_seconds == 1800

    def test_a_change_survives_a_restart(self, tmp_path: Path, clock: FakeClock) -> None:
        engine = build(tmp_path, clock)
        ask(engine, "config.set", key="phrase_length", value="200")

        again = build(tmp_path, clock)
        assert again.policy.phrase_length == 200
        assert setting(again, "phrase_length")["is_default"] is False

    def test_retention_reaches_the_statistics_database(self, engine: Engine) -> None:
        ask(engine, "config.set", key="retention_days", value="7")
        assert engine.stats.retention_days == 7

    def test_a_value_the_setting_cannot_hold_is_refused(self, engine: Engine) -> None:
        response = ask(engine, "config.set", key="retention_days", value="0")
        assert not response.ok
        assert response.code == ErrorCode.INVALID_CONFIG
        assert "at least 1" in response.error["message"]

    def test_subscribers_are_told(self, engine: Engine) -> None:
        seen: list[Any] = []
        engine.subscribe(seen.append)
        ask(engine, "config.set", key="language", value="es")
        assert [(event.event, event.payload) for event in seen] == [
            ("config.changed", {"what": "settings", "key": "language"})
        ]


class TestDuringASession:
    def test_the_firm_wait_will_not_change_mid_session(self, engine: Engine) -> None:
        start(engine)
        response = ask(engine, "config.set", key="firm_wait_seconds", value="60")
        assert not response.ok
        assert response.code == ErrorCode.SETTING_LOCKED
        assert engine.policy.firm_wait_seconds == DEFAULT_FIRM_WAIT_SECONDS

    def test_not_even_to_something_stricter(self, engine: Engine) -> None:
        # SPEC 7.2 says "never during a session", not "never looser": a
        # session's exit cost is settled when it starts, in both directions.
        start(engine)
        response = ask(engine, "config.set", key="phrase_length", value="900")
        assert not response.ok
        assert response.code == ErrorCode.SETTING_LOCKED

    def test_retention_and_language_still_change(self, engine: Engine) -> None:
        start(engine)
        assert ask(engine, "config.set", key="language", value="es").ok
        assert ask(engine, "config.set", key="retention_days", value="30").ok

    def test_the_wait_changes_again_once_the_session_is_over(self, engine: Engine) -> None:
        start(engine, level="soft")
        engine._end_session(reason="completed")
        assert ask(engine, "config.set", key="firm_wait_seconds", value="60").ok
