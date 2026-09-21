"""Editing a category, and where the edit lands (SPEC 12, 15, 7.4)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from anchor.engine.core import Engine
from anchor.engine.paths import Paths, Settings
from anchor.engine.profiles import Profile
from anchor.engine.timekeeping import FakeClock
from anchor.protocol.errors import ErrorCode
from anchor.protocol.messages import Request, new_id

SHIPPED = """\
name = "Social media"
domains = ["x.com", "facebook.com"]
apps = ["discord.desktop"]
"""


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(wall_time=1_760_000_000.0, boottime=1_000.0, boot="boot-a")


@pytest.fixture
def engine(tmp_path: Path, clock: FakeClock) -> Engine:
    paths = Paths.resolve(tmp_path)
    paths.ensure_directories()
    paths.shipped_categories.mkdir(parents=True, exist_ok=True)
    (paths.shipped_categories / "social.toml").write_text(SHIPPED, encoding="utf-8")

    built = Engine(
        paths,
        Settings(owner_uid=os.getuid()),
        clock=clock,
        systemd_runtime_dir=tmp_path / "run" / "systemd" / "system",
    )
    built.load()
    built.profiles["Study"] = Profile(name="Study", categories=frozenset({"social"}))
    built.save_config()
    return built


def ask(engine: Engine, type_: str, **payload: Any) -> Any:
    request = Request.from_dict({"v": 1, "id": new_id(), "type": type_, "payload": payload})
    return engine.handle(request)


def social(engine: Engine) -> Any:
    return engine.categories["social"]


class TestEditing:
    def test_a_domain_can_be_added(self, engine: Engine) -> None:
        response = ask(engine, "category.edit", id="social", add_domains=["reddit.com"])

        assert response.ok, response.error
        assert "reddit.com" in social(engine).domains

    def test_a_domain_can_be_taken_out(self, engine: Engine) -> None:
        ask(engine, "category.edit", id="social", remove_domains=["x.com"])

        assert "x.com" not in social(engine).domains

    def test_domains_are_stored_in_lower_case(self, engine: Engine) -> None:
        ask(engine, "category.edit", id="social", add_domains=["Reddit.COM"])

        assert "reddit.com" in social(engine).domains

    def test_an_application_can_be_bundled_with_it(self, engine: Engine) -> None:
        """SPEC 9's example: blocking Discord without discord.com blocks little."""
        ask(engine, "category.edit", id="social", add_apps=["telegram.desktop"])

        assert "telegram.desktop" in social(engine).apps

    def test_a_category_nobody_has_is_refused(self, engine: Engine) -> None:
        response = ask(engine, "category.edit", id="nope", add_domains=["x.com"])

        assert not response.ok
        assert response.code == ErrorCode.INVALID_CONFIG

    def test_subscribers_are_told(self, engine: Engine) -> None:
        seen: list[Any] = []
        engine.subscribe(seen.append)
        ask(engine, "category.edit", id="social", add_domains=["reddit.com"])

        assert [event.event for event in seen] == ["config.changed"]


class TestWhereItIsWritten:
    def test_the_user_s_copy_is_written_not_the_package_s(self, engine: Engine) -> None:
        """SPEC 12: shipped lists stay current, and the user's are their own."""
        ask(engine, "category.edit", id="social", add_domains=["reddit.com"])

        assert (engine.paths.user_categories / "social.toml").exists()
        assert "reddit.com" not in (engine.paths.shipped_categories / "social.toml").read_text(
            encoding="utf-8"
        )

    def test_and_is_what_the_engine_reads_next_time(self, engine: Engine) -> None:
        ask(engine, "category.edit", id="social", add_domains=["reddit.com"])

        again = Engine(
            engine.paths,
            Settings(owner_uid=os.getuid()),
            clock=engine.clock,
            systemd_runtime_dir=engine.systemd_runtime_dir,
        )
        again.load()
        assert "reddit.com" in again.categories["social"].domains

    def test_a_package_update_does_not_undo_it(self, engine: Engine) -> None:
        ask(engine, "category.edit", id="social", add_domains=["reddit.com"])
        # What an upgrade does: replace the shipped file.
        (engine.paths.shipped_categories / "social.toml").write_text(
            'name = "Social media"\ndomains = ["x.com"]\n', encoding="utf-8"
        )

        again = Engine(
            engine.paths,
            Settings(owner_uid=os.getuid()),
            clock=engine.clock,
            systemd_runtime_dir=engine.systemd_runtime_dir,
        )
        again.load()
        assert "reddit.com" in again.categories["social"].domains


class TestDuringASession:
    def start(self, engine: Engine) -> None:
        response = ask(
            engine, "session.start", profile="Study", duration_seconds=3600, level="firm"
        )
        assert response.ok, response.error

    def test_adding_to_a_blocked_category_is_allowed(self, engine: Engine) -> None:
        self.start(engine)
        response = ask(engine, "category.edit", id="social", add_domains=["reddit.com"])

        assert response.ok, response.error

    def test_taking_something_out_of_one_is_not(self, engine: Engine) -> None:
        """Otherwise a session could be unwound one category entry at a time."""
        self.start(engine)
        response = ask(engine, "category.edit", id="social", remove_domains=["x.com"])

        assert not response.ok
        assert response.code == ErrorCode.RATCHET_VIOLATION
        assert "x.com" in social(engine).domains

    def test_a_category_the_session_does_not_use_is_free(self, engine: Engine) -> None:
        (engine.paths.shipped_categories / "news.toml").write_text(
            'name = "News"\ndomains = ["bbc.com"]\n', encoding="utf-8"
        )
        engine.load()
        self.start(engine)

        response = ask(engine, "category.edit", id="news", remove_domains=["bbc.com"])
        assert response.ok, response.error
