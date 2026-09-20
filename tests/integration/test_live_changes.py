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
from typing import Any

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


class TestWhenTunnelsAreBlocked:
    """SPEC 7.2 allows VPN below Strict; ADR 4 lets a profile opt out above it."""

    def policy(self, client: EngineClient) -> dict[str, Any]:
        return client.call("policy.get").result

    def test_a_soft_session_does_not_block_them(self, client: EngineClient) -> None:
        client.call(
            "session.start",
            {"profile": "Study", "duration_seconds": HOUR, "level": "soft"},
        )
        assert self.policy(client)["block_tunnels"] is False

    def test_a_firm_session_does_not_block_them(self, client: EngineClient) -> None:
        client.call(
            "session.start",
            {"profile": "Study", "duration_seconds": HOUR, "level": "firm"},
        )
        assert self.policy(client)["block_tunnels"] is False

    def test_a_strict_session_blocks_them(self, client: EngineClient) -> None:
        client.call(
            "session.start",
            {
                "profile": "Study",
                "duration_seconds": HOUR,
                "level": "strict",
                "valve": "wait",
            },
        )
        assert self.policy(client)["block_tunnels"] is True

    def test_a_profile_can_opt_out_before_the_session(self, client: EngineClient) -> None:
        """ADR 4: the choice is made beforehand, and this is beforehand."""
        client.call("profile.edit", {"name": "Study", "block_vpn_and_tor": False})
        client.call(
            "session.start",
            {
                "profile": "Study",
                "duration_seconds": HOUR,
                "level": "strict",
                "valve": "wait",
            },
        )

        assert self.policy(client)["block_tunnels"] is False

    def test_a_missing_profile_still_blocks_them_in_strict(
        self, client: EngineClient, engine: Engine
    ) -> None:
        """A profile that vanished must not become a way to keep a tunnel up."""
        client.call(
            "session.start",
            {
                "profile": "Study",
                "duration_seconds": HOUR,
                "level": "strict",
                "valve": "wait",
            },
        )
        del engine.profiles["Study"]

        assert self.policy(client)["block_tunnels"] is True


class TestWhatTheBlockerIsToldAboutApplications:
    """SPEC 7.1 and 9: which applications, and how long they have left."""

    def policy(self, client: EngineClient) -> dict[str, Any]:
        return client.call("policy.get").result

    def test_the_profiles_applications_are_named(self, client: EngineClient) -> None:
        start(client)
        assert self.policy(client)["apps"] == ["discord"]

    def test_a_session_starts_with_the_full_two_minutes(self, client: EngineClient) -> None:
        start(client)
        assert self.policy(client)["grace_seconds"] == 120

    def test_the_grace_runs_down_with_the_session(
        self, client: EngineClient, clock: FakeClock
    ) -> None:
        start(client)
        clock.advance(90)

        assert self.policy(client)["grace_seconds"] == 30

    def test_it_reaches_zero_and_stays_there(self, client: EngineClient, clock: FakeClock) -> None:
        start(client)
        clock.advance(600)

        assert self.policy(client)["grace_seconds"] == 0

    def test_restarting_the_blocker_does_not_hand_out_a_fresh_grace(
        self, client: EngineClient, clock: FakeClock
    ) -> None:
        """The engine owns the deadline, so asking again cannot reset it."""
        start(client)
        clock.advance(119)

        first = self.policy(client)["grace_seconds"]
        second = self.policy(client)["grace_seconds"]

        assert first == second == 1

    def test_a_missing_profile_names_no_applications(
        self, client: EngineClient, engine: Engine
    ) -> None:
        """Closing every application on the machine is not a safe guess."""
        start(client)
        del engine.profiles["Study"]

        assert self.policy(client)["apps"] == []

    def test_an_application_added_mid_session_appears(self, client: EngineClient) -> None:
        start(client)
        client.call("profile.edit", {"name": "Study", "add_apps": ["spotify"]})

        assert self.policy(client)["apps"] == ["discord", "spotify"]


class TestReportingApplications:
    def test_the_grace_warning_is_published(self, client: EngineClient) -> None:
        start(client)

        response = client.call(
            "apps.report", {"kind": "grace", "apps": ["Discord"], "seconds": 120}
        )

        assert response.ok
        assert response.result == {"recorded": True}

    def test_a_closed_application_is_counted(self, client: EngineClient) -> None:
        """SPEC 13 asks for blocked attempts by application as well as by domain."""
        start(client)

        client.call("apps.report", {"kind": "closed", "apps": ["Discord", "Spotify"]})
        response = client.call("apps.report", {"kind": "launch", "apps": ["Discord"]})

        assert response.result["total"] == 3

    def test_the_count_is_kept_apart_from_blocked_domains(self, client: EngineClient) -> None:
        start(client)
        client.call("apps.report", {"kind": "closed", "apps": ["Discord"]})

        status = client.call("status.get").result
        assert status["app_blocks"] == 1
        assert status["blocked_attempts"] == 0

    def test_a_report_with_no_session_is_not_an_error(self, client: EngineClient) -> None:
        """The session can end between the kill and the report."""
        response = client.call("apps.report", {"kind": "closed", "apps": ["Discord"]})

        assert response.ok
        assert response.result == {"recorded": False}

    def test_an_unknown_kind_is_refused(self, client: EngineClient) -> None:
        start(client)
        response = client.call("apps.report", {"kind": "banished", "apps": ["Discord"]})

        assert not response.ok
