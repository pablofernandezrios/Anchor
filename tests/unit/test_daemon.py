"""The blocker daemon's two transitions: applying and undoing (SPEC 5.1)."""

from __future__ import annotations

from pathlib import Path

import pytest

import anchor.blocker.resolved as resolved_module
from anchor.blocker.daemon import BlockerDaemon, Lists
from anchor.engine.paths import Paths
from anchor.protocol.types import WebMode
from anchor.system.commands import RecordingRunner, Result

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
def lists() -> Lists:
    data = Path(__file__).resolve().parents[2] / "data"
    return Lists.load(data)


@pytest.fixture
def runner() -> RecordingRunner:
    """A machine with systemd-resolved running."""
    return RecordingRunner(
        {
            "resolvectl status": Result(code=0, out=STATUS),
            "is-active": Result(code=0, out="active"),
        }
    )


@pytest.fixture
def daemon(paths: Paths, lists: Lists, runner: RecordingRunner, tmp_path: Path) -> BlockerDaemon:
    return BlockerDaemon(
        paths,
        lists=lists,
        runner=runner,
        resolver_port=5391,
        policy_root=tmp_path / "root",
        # Every path the daemon writes is redirected here. Without this the
        # tests write to the real /run/systemd, which passes as root and fails
        # for everyone else.
        resolved_drop_in=tmp_path / "run" / "systemd" / "resolved.conf.d" / "50-anchor.conf",
    )


class TestApplying:
    def test_the_firewall_is_loaded(self, daemon: BlockerDaemon, runner: RecordingRunner) -> None:
        daemon.apply(WebMode.BLOCKLIST, frozenset({"youtube.com"}))

        assert runner.ran("nft -f")
        assert daemon.applied

    def test_the_users_domains_are_blocked(self, daemon: BlockerDaemon) -> None:
        daemon.apply(WebMode.BLOCKLIST, frozenset({"youtube.com"}))

        policy = daemon.current_policy()
        assert policy is not None
        assert policy.is_blocked("www.youtube.com")

    def test_doh_names_are_blocked_alongside_them(self, daemon: BlockerDaemon) -> None:
        """A browser looking up its DoH provider must not find it."""
        daemon.apply(WebMode.BLOCKLIST, frozenset({"youtube.com"}))

        policy = daemon.current_policy()
        assert policy is not None
        assert policy.is_blocked("dns.google")
        assert policy.is_blocked("mozilla.cloudflare-dns.com")

    def test_essentials_still_resolve(self, daemon: BlockerDaemon) -> None:
        daemon.apply(WebMode.ALLOWLIST, frozenset())

        policy = daemon.current_policy()
        assert policy is not None
        assert not policy.is_blocked("connectivity-check.ubuntu.com")

    def test_resolved_is_pointed_at_anchor(
        self, daemon: BlockerDaemon, runner: RecordingRunner
    ) -> None:
        daemon.apply(WebMode.BLOCKLIST, frozenset({"youtube.com"}))
        assert runner.ran("resolvectl dns eth0 127.0.0.1:5391")

    def test_browser_policies_are_written(self, daemon: BlockerDaemon, tmp_path: Path) -> None:
        firefox = tmp_path / "root" / "usr" / "bin" / "firefox"
        firefox.parent.mkdir(parents=True)
        firefox.write_text("#!/bin/sh\n", encoding="utf-8")

        daemon.apply(WebMode.BLOCKLIST, frozenset({"youtube.com"}))

        policy_file = tmp_path / "root" / "etc" / "firefox" / "policies" / "policies.json"
        assert policy_file.exists()

    def test_applying_the_same_policy_again_does_nothing(
        self, daemon: BlockerDaemon, runner: RecordingRunner
    ) -> None:
        """The daemon polls every second; it must not reload rules every time."""
        daemon.apply(WebMode.BLOCKLIST, frozenset({"youtube.com"}))
        loads = sum(1 for call in runner.calls if "-f" in call)

        daemon.apply(WebMode.BLOCKLIST, frozenset({"youtube.com"}))
        assert sum(1 for call in runner.calls if "-f" in call) == loads

    def test_a_changed_policy_is_reapplied(
        self, daemon: BlockerDaemon, runner: RecordingRunner
    ) -> None:
        daemon.apply(WebMode.BLOCKLIST, frozenset({"youtube.com"}))
        loads = sum(1 for call in runner.calls if "-f" in call)

        daemon.apply(WebMode.BLOCKLIST, frozenset({"youtube.com", "reddit.com"}))
        assert sum(1 for call in runner.calls if "-f" in call) > loads


class TestWithoutAnUpstream:
    """The bug the first full end-to-end run found."""

    def test_nothing_is_redirected_when_no_server_can_be_found(
        self, paths: Paths, lists: Lists, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Redirecting with nowhere to forward blocks the entire internet."""
        empty = tmp_path / "resolv.conf"
        empty.write_text("# nothing here\n", encoding="utf-8")
        monkeypatch.setattr(resolved_module, "RESOLV_CONF", empty)

        runner = RecordingRunner({"resolvectl": Result(code=127, err="not found")})
        daemon = BlockerDaemon(
            paths,
            lists=lists,
            runner=runner,
            resolver_port=5391,
            resolved_drop_in=tmp_path / "run" / "50-anchor.conf",
        )

        daemon.apply(WebMode.BLOCKLIST, frozenset({"youtube.com"}))

        assert not runner.ran("nft -f"), "DNS was redirected with nowhere to forward"
        assert not daemon.applied
        assert daemon.current_policy() is None

    def test_resolv_conf_is_enough_on_its_own(
        self, paths: Paths, lists: Lists, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A machine without systemd-resolved still blocks (SPEC 8.2)."""
        conf = tmp_path / "resolv.conf"
        conf.write_text("nameserver 8.8.8.8\n", encoding="utf-8")
        monkeypatch.setattr(resolved_module, "RESOLV_CONF", conf)

        runner = RecordingRunner({"resolvectl": Result(code=127, err="not found")})
        daemon = BlockerDaemon(
            paths,
            lists=lists,
            runner=runner,
            resolver_port=5391,
            resolved_drop_in=tmp_path / "run" / "50-anchor.conf",
        )

        daemon.apply(WebMode.BLOCKLIST, frozenset({"youtube.com"}))

        assert runner.ran("nft -f")
        assert daemon.applied


class TestUndoing:
    def test_everything_is_put_back(self, daemon: BlockerDaemon, runner: RecordingRunner) -> None:
        daemon.apply(WebMode.BLOCKLIST, frozenset({"youtube.com"}))
        daemon.undo()

        assert runner.ran("nft delete table inet anchor")
        assert runner.ran("resolvectl revert eth0")
        assert not daemon.applied
        assert daemon.current_policy() is None

    def test_what_the_user_looked_at_is_forgotten(self, daemon: BlockerDaemon) -> None:
        """The recent-answer map must not outlive the session that needed it."""
        daemon.recent.record("youtube.com", ["142.250.1.1"])
        daemon.apply(WebMode.BLOCKLIST, frozenset({"youtube.com"}))

        daemon.undo()
        assert len(daemon.recent) == 0

    def test_the_attempt_count_starts_again(self, daemon: BlockerDaemon) -> None:
        daemon.attempts.record("youtube.com")
        daemon.undo()

        assert daemon.attempts.total == 0


class TestCountingAttempts:
    def test_the_two_halves_of_one_lookup_count_once(self, daemon: BlockerDaemon) -> None:
        """A visit asks for A and AAAA; the indicator should say one."""
        daemon.on_blocked("youtube.com", "youtube.com")
        daemon.on_blocked("youtube.com", "youtube.com")

        assert daemon.attempts.total == 1

    def test_an_unreachable_engine_does_not_break_a_lookup(self, daemon: BlockerDaemon) -> None:
        """Losing a statistic is not worth failing a DNS query over."""
        daemon.on_blocked("youtube.com", "youtube.com")  # no engine is listening


class TestNoticingTheRulesAreGone:
    """SPEC 7.6: a missing nftables table is manipulation, and is recorded."""

    def test_a_missing_table_is_noticed(self, paths: Paths, lists: Lists, tmp_path: Path) -> None:
        # nft list fails, which is what an absent table looks like.
        runner = RecordingRunner(
            {
                "resolvectl status": Result(code=0, out=STATUS),
                "is-active": Result(code=0, out="active"),
                "nft list table": Result(code=1, err="No such file or directory"),
            }
        )
        daemon = BlockerDaemon(
            paths,
            lists=lists,
            runner=runner,
            resolver_port=5391,
            resolved_drop_in=tmp_path / "run" / "50-anchor.conf",
        )
        daemon.apply(WebMode.BLOCKLIST, frozenset({"youtube.com"}))
        assert daemon.current_policy() is not None

        daemon.check_rules_survive()

        # Forgotten, so the next poll rebuilds rather than deciding nothing
        # has changed.
        assert daemon.current_policy() is None
        assert daemon.applied is False

    def test_a_table_that_is_still_there_is_left_alone(
        self, daemon: BlockerDaemon, runner: RecordingRunner
    ) -> None:
        daemon.apply(WebMode.BLOCKLIST, frozenset({"youtube.com"}))
        before = len(runner.calls)

        daemon.check_rules_survive()

        assert daemon.applied
        # Only the check itself, no rebuild.
        assert len(runner.calls) == before + 1

    def test_nothing_is_checked_when_no_session_is_running(
        self, daemon: BlockerDaemon, runner: RecordingRunner
    ) -> None:
        """With no rules applied there is nothing to have been removed."""
        daemon.check_rules_survive()
        assert not runner.ran("nft list table")
