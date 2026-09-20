"""Editing profiles while a session runs (SPEC 7.4, P3).

The ratchet logic was built and tested in Milestone 1. This is about the part
that matters in use: that a real edit, arriving over the socket from a real
client, is checked against it before anything is written.
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
from anchor.engine.profiles import Profile
from anchor.engine.service import EngineServer
from anchor.engine.timekeeping import FakeClock
from anchor.protocol.errors import ErrorCode
from anchor.protocol.types import WebMode

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
def engine(paths: Paths, clock: FakeClock, tmp_path: Path) -> Engine:
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
        domains=frozenset({"youtube.com", "reddit.com"}),
        apps=frozenset({"discord"}),
        categories=frozenset({"social"}),
    )
    engine.profiles["Other"] = Profile(name="Other", domains=frozenset({"news.com"}))
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


def start(client: EngineClient, profile: str = "Study") -> None:
    response = client.call(
        "session.start",
        {"profile": profile, "duration_seconds": 2 * HOUR, "level": "firm"},
    )
    assert response.ok, response.error


class TestReadingProfiles:
    def test_they_can_be_listed(self, client: EngineClient) -> None:
        response = client.call("profile.list")
        assert response.result["profiles"] == ["Other", "Study"]

    def test_one_can_be_shown(self, client: EngineClient) -> None:
        response = client.call("profile.show", {"name": "Study"})

        assert response.result["profile"]["web_mode"] == "blocklist"
        assert "youtube.com" in response.result["profile"]["domains"]

    def test_an_unknown_profile_says_so(self, client: EngineClient) -> None:
        response = client.call("profile.show", {"name": "Nope"})
        assert response.code == ErrorCode.UNKNOWN_PROFILE


class TestEditingOutsideASession:
    def test_anything_goes(self, client: EngineClient) -> None:
        """With nothing running, a profile is just configuration."""
        response = client.call(
            "profile.edit",
            {"name": "Study", "remove_domains": ["youtube.com"], "remove_apps": ["discord"]},
        )

        assert response.ok
        assert "youtube.com" not in response.result["profile"]["domains"]

    def test_the_mode_can_be_switched_back(self, client: EngineClient) -> None:
        client.call("profile.edit", {"name": "Study", "web_mode": "allowlist"})
        response = client.call("profile.edit", {"name": "Study", "web_mode": "blocklist"})

        assert response.ok

    def test_tunnel_blocking_can_be_turned_off(self, client: EngineClient) -> None:
        """ADR 4: the choice is made before a session, and this is before."""
        response = client.call("profile.edit", {"name": "Study", "block_vpn_and_tor": False})
        assert response.ok
        assert response.result["profile"]["block_vpn_and_tor"] is False


class TestEditingDuringASession:
    def test_adding_a_domain_is_accepted(self, client: EngineClient) -> None:
        start(client)
        response = client.call("profile.edit", {"name": "Study", "add_domains": ["x.com"]})

        assert response.ok
        assert "x.com" in response.result["profile"]["domains"]

    def test_adding_an_app_is_accepted(self, client: EngineClient) -> None:
        start(client)
        response = client.call("profile.edit", {"name": "Study", "add_apps": ["steam"]})

        assert response.ok
        assert "steam" in response.result["profile"]["apps"]

    def test_removing_a_domain_is_refused(self, client: EngineClient) -> None:
        start(client)
        response = client.call("profile.edit", {"name": "Study", "remove_domains": ["youtube.com"]})

        assert response.code == ErrorCode.RATCHET_VIOLATION
        assert "youtube.com" in response.error["message"]

    def test_removing_an_app_is_refused(self, client: EngineClient) -> None:
        start(client)
        assert (
            client.call("profile.edit", {"name": "Study", "remove_apps": ["discord"]}).code
            == ErrorCode.RATCHET_VIOLATION
        )

    def test_removing_a_category_is_refused(self, client: EngineClient) -> None:
        start(client)
        assert (
            client.call("profile.edit", {"name": "Study", "remove_categories": ["social"]}).code
            == ErrorCode.RATCHET_VIOLATION
        )

    def test_turning_tunnel_blocking_off_is_refused(self, client: EngineClient) -> None:
        """ADR 4: it is a decision taken beforehand, not an exit in the moment."""
        start(client)
        response = client.call("profile.edit", {"name": "Study", "block_vpn_and_tor": False})

        assert response.code == ErrorCode.RATCHET_VIOLATION
        assert "VPN" in response.error["message"]

    def test_switching_to_an_allowlist_is_accepted(self, client: EngineClient) -> None:
        """An allowlist blocks everything it does not name, so it is stricter."""
        start(client)
        assert client.call("profile.edit", {"name": "Study", "web_mode": "allowlist"}).ok

    def test_switching_back_to_a_blocklist_is_refused(self, client: EngineClient) -> None:
        client.call("profile.edit", {"name": "Study", "web_mode": "allowlist"})
        start(client)

        response = client.call("profile.edit", {"name": "Study", "web_mode": "blocklist"})
        assert response.code == ErrorCode.RATCHET_VIOLATION

    def test_a_refused_edit_changes_nothing(self, client: EngineClient) -> None:
        """A rejected change must not be half-applied."""
        start(client)
        client.call(
            "profile.edit",
            {"name": "Study", "add_domains": ["x.com"], "remove_domains": ["youtube.com"]},
        )

        shown = client.call("profile.show", {"name": "Study"}).result["profile"]
        assert "youtube.com" in shown["domains"]
        assert "x.com" not in shown["domains"], "part of a refused edit was applied"

    def test_a_refused_edit_is_not_written_to_disk(
        self, client: EngineClient, paths: Paths, clock: FakeClock, tmp_path: Path
    ) -> None:
        start(client)
        client.call("profile.edit", {"name": "Study", "remove_domains": ["youtube.com"]})

        revived = Engine(
            paths,
            Settings(owner_uid=os.getuid()),
            clock=clock,
            systemd_runtime_dir=tmp_path / "run" / "systemd" / "system",
        )
        revived.load()
        assert "youtube.com" in revived.profiles["Study"].domains


class TestOtherProfiles:
    def test_a_profile_not_in_use_can_be_edited_freely(self, client: EngineClient) -> None:
        """The ratchet protects the running session, not the whole file."""
        start(client, profile="Study")

        response = client.call("profile.edit", {"name": "Other", "remove_domains": ["news.com"]})
        assert response.ok

    def test_the_profile_in_use_cannot_be_deleted(self, client: EngineClient) -> None:
        start(client)
        response = client.call("profile.delete", {"name": "Study"})

        assert response.code == ErrorCode.RATCHET_VIOLATION

    def test_another_profile_can_be_deleted(self, client: EngineClient) -> None:
        start(client)
        assert client.call("profile.delete", {"name": "Other"}).ok


class TestCreating:
    def test_a_profile_can_be_created(self, client: EngineClient) -> None:
        response = client.call("profile.create", {"name": "Reading", "domains": ["twitter.com"]})

        assert response.ok
        assert "Reading" in client.call("profile.list").result["profiles"]

    def test_creating_one_during_a_session_is_fine(self, client: EngineClient) -> None:
        """It is not in force, so it loosens nothing."""
        start(client)
        assert client.call("profile.create", {"name": "Reading"}).ok

    def test_a_duplicate_name_is_refused(self, client: EngineClient) -> None:
        response = client.call("profile.create", {"name": "Study"})
        assert response.code == ErrorCode.INVALID_CONFIG
