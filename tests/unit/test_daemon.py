"""The blocker daemon's two transitions: applying and undoing (SPEC 5.1)."""

from __future__ import annotations

import os
import pwd
from pathlib import Path

import pytest

import anchor.blocker.resolved as resolved_module
from anchor.blocker.apps import AppKind, InstalledApp
from anchor.blocker.daemon import BlockerDaemon, Lists
from anchor.blocker.enforcement import Closure
from anchor.engine.paths import Paths, Settings
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


class TestWhatACrashLeftBehind:
    """Rules that outlived the daemon that applied them (P4, SPEC 7.6).

    A SIGKILL, a power cut or an upgrade mid-session leaves the nftables
    table loaded and the browser policies written. The session is over and
    nothing is watching, so the machine is blocked by nobody — and only a
    reboot or `anchor-blockerd --restore` clears it. The daemon looks once,
    when it first hears that no session is running.
    """

    def test_a_table_left_behind_is_cleared(
        self, daemon: BlockerDaemon, runner: RecordingRunner
    ) -> None:
        # The default runner answers `nft list table` with success, which is
        # what a leftover table looks like.
        assert daemon.forget_leftovers() is True
        assert runner.ran("nft delete table inet anchor")

    def test_a_clean_machine_is_left_alone(self, paths: Path, lists: Lists, tmp_path: Path) -> None:
        clean = RecordingRunner({"list table": Result(code=1, err="No such file or directory")})
        daemon = BlockerDaemon(paths, lists=lists, runner=clean, resolver_port=5391)  # type: ignore[arg-type]

        assert daemon.forget_leftovers() is False
        assert not clean.ran("nft delete table")

    def test_it_only_looks_once(self, daemon: BlockerDaemon, runner: RecordingRunner) -> None:
        """One `nft` call, not one a second for the rest of the login."""
        daemon.forget_leftovers()
        runner.calls.clear()

        assert daemon.forget_leftovers() is False
        assert runner.calls == []

    def test_blocks_this_daemon_applied_are_not_leftovers(
        self, daemon: BlockerDaemon, runner: RecordingRunner
    ) -> None:
        """Those are undone by the ordinary path, which knows what it wrote."""
        daemon.apply(WebMode.BLOCKLIST, frozenset({"youtube.com"}))
        runner.calls.clear()

        assert daemon.forget_leftovers() is False


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


class TestApplications:
    """The daemon's half of application blocking (SPEC 7.1, 9)."""

    def test_the_watcher_starts_with_the_session(self, daemon: BlockerDaemon) -> None:
        """It must be watching before the grace ends, not after."""
        daemon.enforce(frozenset(), grace_seconds=120.0)
        try:
            assert daemon._watcher is not None  # noqa: SLF001
        finally:
            daemon.stop_enforcing()

        assert daemon._watcher is None  # noqa: SLF001

    def test_the_same_watcher_is_kept_across_polls(self, daemon: BlockerDaemon) -> None:
        daemon.enforce(frozenset(), grace_seconds=120.0)
        first = daemon._watcher  # noqa: SLF001
        daemon.enforce(frozenset(), grace_seconds=119.0)
        try:
            assert daemon._watcher is first  # noqa: SLF001
        finally:
            daemon.stop_enforcing()

    def test_stopping_when_nothing_is_running_is_harmless(self, daemon: BlockerDaemon) -> None:
        """Every poll without a session calls it."""
        daemon.stop_enforcing()
        daemon.stop_enforcing()

    def test_undoing_a_session_stops_enforcing(self, daemon: BlockerDaemon) -> None:
        daemon.apply(WebMode.BLOCKLIST, frozenset({"youtube.com"}))
        daemon.enforce(frozenset(), grace_seconds=0.0)

        daemon.undo()

        assert daemon._watcher is None  # noqa: SLF001
        assert not daemon.enforcer.enforcing


class TestReportingApplications:
    def sent(self, daemon: BlockerDaemon) -> list[tuple[str, dict[str, object]]]:
        calls: list[tuple[str, dict[str, object]]] = []
        daemon.tell_engine = lambda request, payload: calls.append((request, payload))  # type: ignore[method-assign]
        return calls

    def test_the_grace_names_the_applications(self, daemon: BlockerDaemon) -> None:
        calls = self.sent(daemon)

        daemon.report_grace([_discord()], 120.0)

        assert calls == [("apps.report", {"kind": "grace", "apps": ["Discord"], "seconds": 120})]

    def test_a_closure_says_it_was_already_running(self, daemon: BlockerDaemon) -> None:
        calls = self.sent(daemon)

        daemon.report_closed([_closure()], "running")

        assert calls == [("apps.report", {"kind": "closed", "apps": ["Discord"]})]

    def test_a_launch_says_so(self, daemon: BlockerDaemon) -> None:
        """The agent words the two differently, so the engine is told which."""
        calls = self.sent(daemon)

        daemon.report_closed([_closure()], "launch")

        assert calls == [("apps.report", {"kind": "launch", "apps": ["Discord"]})]

    def test_an_unreachable_engine_is_not_an_error(self, daemon: BlockerDaemon) -> None:
        """The application is already closed; the notification is not worth a crash."""
        daemon.tell_engine("apps.report", {"kind": "closed", "apps": ["Discord"]})


class TestFindingTheOwnersApplications:
    def test_the_owners_home_is_used(self, daemon: BlockerDaemon, paths: Paths) -> None:
        """Root's own home holds none of the user's desktop entries."""
        paths.settings_file.write_text(Settings(owner_uid=os.getuid()).to_toml(), encoding="utf-8")

        assert daemon.owner_home() == Path(pwd.getpwuid(os.getuid()).pw_dir)

    def test_a_missing_settings_file_is_survivable(
        self, daemon: BlockerDaemon, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Discovery then falls back, rather than the daemon refusing to run."""
        with caplog.at_level("WARNING", logger="anchor-blockerd"):
            assert daemon.owner_home() is None

        assert "home" in caplog.text

    def test_an_owner_uid_with_no_account_is_survivable(
        self, daemon: BlockerDaemon, paths: Paths
    ) -> None:
        paths.settings_file.write_text(Settings(owner_uid=65123).to_toml(), encoding="utf-8")

        assert daemon.owner_home() is None


def _discord() -> InstalledApp:
    return InstalledApp(
        id="discord.desktop",
        name="Discord",
        kind=AppKind.NATIVE,
        exec_path="/usr/bin/discord",
    )


def _closure() -> Closure:
    return Closure(app_id="discord.desktop", name="Discord", pids=(100,), killed=False)
