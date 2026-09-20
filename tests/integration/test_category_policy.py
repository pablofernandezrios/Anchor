"""What a category actually blocks, over the socket (SPEC 9, 12).

The unit tests read category files. This checks the part that matters in use:
that ticking "Social media" in a profile reaches the blocker as domains *and*
as an application, and that it does the right thing in an allowlist.
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
from anchor.protocol.types import WebMode

HOUR = 3600

SOCIAL = """\
name = "Social media"
domains = ["discord.com", "facebook.com"]
apps = ["discord.desktop"]
"""

VIDEO = """\
name = "Video"
domains = ["youtube.com"]
apps = ["mpv.desktop"]
"""


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    tree = Paths.resolve(tmp_path)
    tree.ensure_directories()
    tree.shipped_categories.mkdir(parents=True, exist_ok=True)
    (tree.shipped_categories / "social.toml").write_text(SOCIAL, encoding="utf-8")
    (tree.shipped_categories / "video.toml").write_text(VIDEO, encoding="utf-8")
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
        categories=frozenset({"social"}),
        domains=frozenset({"news.example"}),
        apps=frozenset({"steam.desktop"}),
    )
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


def start(client: EngineClient, level: str = "firm") -> None:
    response = client.call(
        "session.start",
        {"profile": "Study", "duration_seconds": HOUR, "level": level},
    )
    assert response.ok, response.error


def policy(client: EngineClient) -> dict[str, Any]:
    response = client.call("policy.get")
    assert response.ok, response.error
    return response.result


class TestABlocklistProfile:
    def test_the_categorys_domains_are_blocked(self, client: EngineClient) -> None:
        start(client)

        assert set(policy(client)["domains"]) == {
            "discord.com",
            "facebook.com",
            "news.example",
        }

    def test_the_categorys_application_is_blocked_too(self, client: EngineClient) -> None:
        """SPEC 9: 'Discord' blocks the app and discord.com."""
        start(client)

        assert set(policy(client)["apps"]) == {"discord.desktop", "steam.desktop"}

    def test_the_users_own_domains_survive(self, client: EngineClient) -> None:
        start(client)

        assert "news.example" in policy(client)["domains"]

    def test_a_category_added_during_a_session_takes_effect(self, client: EngineClient) -> None:
        """The ratchet allows adding, and adding must mean something."""
        start(client)
        response = client.call("profile.edit", {"name": "Study", "add_categories": ["video"]})
        assert response.ok, response.error

        assert "youtube.com" in policy(client)["domains"]
        assert "mpv.desktop" in policy(client)["apps"]

    def test_a_category_nothing_answers_to_is_skipped(
        self, client: EngineClient, engine: Engine
    ) -> None:
        """A deleted list must not stop a session from blocking the rest."""
        engine.profiles["Study"] = Profile(
            name="Study",
            web_mode=WebMode.BLOCKLIST,
            categories=frozenset({"social", "gone"}),
            domains=frozenset({"news.example"}),
        )
        engine.save_config()
        start(client)

        assert "discord.com" in policy(client)["domains"]


class TestAnAllowlistProfile:
    """The sense inverts for domains, and does not for applications."""

    def allowlist(self, engine: Engine) -> None:
        engine.profiles["Study"] = Profile(
            name="Study",
            web_mode=WebMode.ALLOWLIST,
            categories=frozenset({"social"}),
            domains=frozenset({"wikipedia.org"}),
        )
        engine.save_config()

    def test_a_categorys_domains_are_not_added_to_what_is_allowed(
        self, client: EngineClient, engine: Engine
    ) -> None:
        """Otherwise 'block social media' would make it the only thing readable."""
        self.allowlist(engine)
        start(client)

        assert policy(client)["domains"] == ["wikipedia.org"]

    def test_the_categorys_application_is_still_blocked(
        self, client: EngineClient, engine: Engine
    ) -> None:
        """SPEC 9: applications are blocklist only, whatever the web mode."""
        self.allowlist(engine)
        start(client)

        assert policy(client)["apps"] == ["discord.desktop"]


class TestListingThem:
    def test_the_installed_categories_are_listed(self, client: EngineClient) -> None:
        response = client.call("category.list")

        assert response.ok
        names = [category["name"] for category in response.result["categories"]]
        assert names == ["Social media", "Video"]

    def test_each_one_carries_its_domains_and_apps(self, client: EngineClient) -> None:
        categories = client.call("category.list").result["categories"]
        social = next(c for c in categories if c["id"] == "social")

        assert social["domains"] == ["discord.com", "facebook.com"]
        assert social["apps"] == ["discord.desktop"]

    def test_it_works_with_no_session(self, client: EngineClient) -> None:
        """The interface shows the tick boxes before anything is running."""
        assert client.call("category.list").ok


class TestTheUsersOwnCopies:
    def test_a_copy_in_etc_replaces_the_shipped_one(
        self, paths: Paths, engine: Engine, client: EngineClient
    ) -> None:
        paths.user_categories.mkdir(parents=True, exist_ok=True)
        (paths.user_categories / "social.toml").write_text(
            'name = "Social media"\ndomains = ["only-this.example"]\n', encoding="utf-8"
        )
        engine.load()
        start(client)

        assert set(policy(client)["domains"]) == {"only-this.example", "news.example"}
