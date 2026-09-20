"""Closing blocked applications, and the grace before it (SPEC 7.1, 9)."""

from __future__ import annotations

import signal
from pathlib import Path

import pytest

from anchor.blocker.apps import AppKind, InstalledApp
from anchor.blocker.enforcement import (
    GRACE_SECONDS,
    KILL_AFTER_SECONDS,
    AppEnforcer,
    Closure,
    escalate,
    is_alive,
)


def app(app_id: str = "discord.desktop", **overrides: object) -> InstalledApp:
    base: dict[str, object] = {
        "id": app_id,
        "name": "Discord",
        "kind": AppKind.NATIVE,
        "exec_path": "/usr/bin/discord",
    }
    base.update(overrides)
    return InstalledApp(**base)  # type: ignore[arg-type]


class FakeProc:
    """A directory shaped like /proc, which tests can change."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def add(self, pid: int, *, exe: str = "", cgroup: str = "", state: str = "S") -> None:
        directory = self.root / str(pid)
        directory.mkdir(parents=True, exist_ok=True)
        if exe:
            link = directory / "exe"
            link.unlink(missing_ok=True)
            link.symlink_to(exe)
        (directory / "cgroup").write_text(cgroup, encoding="utf-8")
        self.set_state(pid, state)

    def set_state(self, pid: int, state: str) -> None:
        # A real stat line: pid, the command in brackets, then the state.
        (self.root / str(pid) / "stat").write_text(
            f"{pid} (some (odd) name) {state} 1 1 0 0", encoding="utf-8"
        )

    def remove(self, pid: int) -> None:
        directory = self.root / str(pid)
        for entry in directory.iterdir():
            entry.unlink()
        directory.rmdir()


@pytest.fixture
def proc(tmp_path: Path) -> FakeProc:
    return FakeProc(tmp_path / "proc")


class Signals:
    """Records signals, and can make a process go away when one arrives."""

    def __init__(self, proc: FakeProc | None = None, *, dies: bool = True) -> None:
        self.sent: list[tuple[int, int]] = []
        self._proc = proc
        self._dies = dies

    def __call__(self, pid: int, number: int) -> None:
        self.sent.append((pid, number))
        if self._proc is None:
            return
        if number == signal.SIGKILL or (self._dies and number == signal.SIGTERM):
            self._proc.remove(pid)


class TestTheConstants:
    def test_the_grace_is_two_minutes(self) -> None:
        assert GRACE_SECONDS == 120

    def test_the_kill_follows_ten_seconds_later(self) -> None:
        assert KILL_AFTER_SECONDS == 10


class TestIsAlive:
    def test_a_running_process_is_alive(self, proc: FakeProc) -> None:
        proc.add(100, exe="/usr/bin/discord")
        assert is_alive(100, proc=proc.root)

    def test_a_process_that_is_gone_is_not(self, proc: FakeProc) -> None:
        proc.root.mkdir(parents=True, exist_ok=True)
        assert not is_alive(999, proc=proc.root)

    def test_a_zombie_is_not_alive(self, proc: FakeProc) -> None:
        """It answers signal 0, so asking the kernel instead would hang."""
        proc.add(100, exe="/usr/bin/discord")
        proc.set_state(100, "Z")

        assert not is_alive(100, proc=proc.root)

    def test_a_command_name_with_brackets_does_not_confuse_it(self, proc: FakeProc) -> None:
        proc.add(100, exe="/usr/bin/discord", state="R")
        assert is_alive(100, proc=proc.root)


class TestEscalation:
    def test_a_process_that_leaves_is_never_killed(self, proc: FakeProc) -> None:
        proc.add(100, exe="/usr/bin/discord")
        signals = Signals(proc)

        killed = escalate(
            [100],
            kill_after=1.0,
            signaller=signals,
            alive=lambda pid: is_alive(pid, proc=proc.root),
        )

        assert not killed
        assert signals.sent == [(100, signal.SIGTERM)]

    def test_a_process_that_stays_is_killed(self, proc: FakeProc) -> None:
        """SPEC 7.1: SIGTERM, then SIGKILL ten seconds later."""
        proc.add(100, exe="/usr/bin/discord")
        signals = Signals(proc, dies=False)

        killed = escalate(
            [100],
            kill_after=0.05,
            signaller=signals,
            alive=lambda pid: is_alive(pid, proc=proc.root),
        )

        assert killed
        assert signals.sent[0] == (100, signal.SIGTERM)
        assert signals.sent[-1] == (100, signal.SIGKILL)

    def test_everything_is_asked_before_anything_is_waited_on(self, proc: FakeProc) -> None:
        """Otherwise closing five windows would take fifty seconds."""
        for pid in (100, 101, 102):
            proc.add(pid, exe="/usr/bin/discord")
        signals = Signals(proc, dies=False)

        escalate(
            [100, 101, 102],
            kill_after=0.05,
            signaller=signals,
            alive=lambda pid: is_alive(pid, proc=proc.root),
        )

        first_three = signals.sent[:3]
        assert first_three == [(pid, signal.SIGTERM) for pid in (100, 101, 102)]

    def test_a_process_that_has_already_gone_is_not_waited_for(self) -> None:
        def gone(pid: int, number: int) -> None:
            raise ProcessLookupError

        assert not escalate([100], kill_after=10.0, signaller=gone, alive=lambda pid: True)

    def test_a_refused_signal_is_not_an_error(self, caplog: pytest.LogCaptureFixture) -> None:
        """Root can still be refused, and the application stays open."""

        def refused(pid: int, number: int) -> None:
            raise PermissionError

        with caplog.at_level("WARNING", logger="anchor-blockerd"):
            assert not escalate([100], kill_after=0.01, signaller=refused)

        assert "100" in caplog.text

    def test_any_other_signal_error_is_survived(self, caplog: pytest.LogCaptureFixture) -> None:
        def broken(pid: int, number: int) -> None:
            raise OSError("the pid namespace went away")

        with caplog.at_level("WARNING", logger="anchor-blockerd"):
            assert not escalate([100], kill_after=0.01, signaller=broken)

        assert "100" in caplog.text

    def test_nothing_to_close_is_not_a_kill(self) -> None:
        assert not escalate([], signaller=lambda pid, number: None)


class Reports:
    def __init__(self) -> None:
        self.grace: list[tuple[list[str], float]] = []
        self.closed: list[tuple[list[str], str]] = []

    def on_grace(self, apps: list[InstalledApp], seconds: float) -> None:
        self.grace.append(([a.name for a in apps], seconds))

    def on_closed(self, closures: list[Closure], reason: str) -> None:
        self.closed.append(([c.name for c in closures], reason))


def enforcer(proc: FakeProc, reports: Reports, signals: Signals, **kwargs: object) -> AppEnforcer:
    settings: dict[str, object] = {
        "catalogue": lambda: [
            app(),
            app(
                "spotify.desktop",
                name="Spotify",
                kind=AppKind.SNAP,
                snap_name="spotify",
                exec_path="/snap/bin/spotify",
            ),
        ],
        "on_grace": reports.on_grace,
        "on_closed": reports.on_closed,
        "signaller": signals,
        "proc": proc.root,
        "kill_after": 0.05,
        # Run the escalation on the calling thread, so the test can see the
        # outcome without waiting on one.
        "spawn": lambda work: work(),
    }
    settings.update(kwargs)
    return AppEnforcer(**settings)  # type: ignore[arg-type]


class TestTheGracePeriod:
    def test_nothing_is_closed_while_the_grace_runs(self, proc: FakeProc) -> None:
        """SPEC 7.1: two minutes to save work, and that means two minutes."""
        proc.add(100, exe="/usr/bin/discord")
        reports, signals = Reports(), Signals(proc)
        subject = enforcer(proc, reports, signals)

        subject.update(frozenset({"discord.desktop"}), grace_remaining=90.0)

        assert signals.sent == []
        assert not subject.enforcing

    def test_the_user_is_told_what_will_close(self, proc: FakeProc) -> None:
        proc.add(100, exe="/usr/bin/discord")
        reports, signals = Reports(), Signals(proc)
        subject = enforcer(proc, reports, signals)

        subject.update(frozenset({"discord.desktop"}), grace_remaining=120.0)

        assert reports.grace == [(["Discord"], 120.0)]

    def test_the_warning_is_given_once(self, proc: FakeProc) -> None:
        """The daemon polls every second; two minutes of that is not a warning."""
        proc.add(100, exe="/usr/bin/discord")
        reports, signals = Reports(), Signals(proc)
        subject = enforcer(proc, reports, signals)

        for remaining in (120.0, 119.0, 118.0):
            subject.update(frozenset({"discord.desktop"}), grace_remaining=remaining)

        assert len(reports.grace) == 1

    def test_one_entry_per_application_not_per_process(self, proc: FakeProc) -> None:
        """A browser is one thing the user has open, not twenty."""
        for pid in (100, 101, 102):
            proc.add(pid, exe="/usr/bin/discord")
        reports, signals = Reports(), Signals(proc)
        subject = enforcer(proc, reports, signals)

        subject.update(frozenset({"discord.desktop"}), grace_remaining=120.0)

        assert reports.grace == [(["Discord"], 120.0)]

    def test_nothing_is_said_when_nothing_is_open(self, proc: FakeProc) -> None:
        proc.root.mkdir(parents=True, exist_ok=True)
        reports, signals = Reports(), Signals(proc)
        subject = enforcer(proc, reports, signals)

        subject.update(frozenset({"discord.desktop"}), grace_remaining=120.0)

        assert reports.grace == []

    def test_an_application_opened_during_the_grace_is_left_alone(self, proc: FakeProc) -> None:
        """It may be the same window the user is saving work in.

        SPEC 9 kills on launch during the session; the grace is what comes
        before the session's blocks bite, and killing inside it would take the
        two minutes back.
        """
        proc.add(100, exe="/usr/bin/discord")
        reports, signals = Reports(), Signals(proc)
        subject = enforcer(proc, reports, signals)
        subject.update(frozenset({"discord.desktop"}), grace_remaining=120.0)

        proc.add(101, exe="/usr/bin/discord")
        subject.on_launch(101)

        assert signals.sent == []


class TestWhenTheGraceEnds:
    def test_running_applications_are_closed(self, proc: FakeProc) -> None:
        proc.add(100, exe="/usr/bin/discord")
        reports, signals = Reports(), Signals(proc)
        subject = enforcer(proc, reports, signals)

        subject.update(frozenset({"discord.desktop"}), grace_remaining=0.0)

        assert (100, signal.SIGTERM) in signals.sent
        assert subject.enforcing

    def test_what_was_closed_is_reported(self, proc: FakeProc) -> None:
        proc.add(100, exe="/usr/bin/discord")
        reports, signals = Reports(), Signals(proc)
        subject = enforcer(proc, reports, signals)

        subject.update(frozenset({"discord.desktop"}), grace_remaining=0.0)

        assert reports.closed == [(["Discord"], "running")]

    def test_other_applications_are_left_alone(self, proc: FakeProc) -> None:
        proc.add(100, exe="/usr/bin/firefox")
        reports, signals = Reports(), Signals(proc)
        subject = enforcer(proc, reports, signals)

        subject.update(frozenset({"discord.desktop"}), grace_remaining=0.0)

        assert signals.sent == []

    def test_a_snap_is_closed_by_its_cgroup(self, proc: FakeProc) -> None:
        proc.add(100, exe="/usr/lib/snapd/snap-confine", cgroup="0::/snap.spotify.spotify.scope")
        reports, signals = Reports(), Signals(proc)
        subject = enforcer(proc, reports, signals)

        subject.update(frozenset({"spotify.desktop"}), grace_remaining=0.0)

        assert (100, signal.SIGTERM) in signals.sent

    def test_every_process_of_one_application_goes(self, proc: FakeProc) -> None:
        for pid in (100, 101, 102):
            proc.add(pid, exe="/usr/bin/discord")
        reports, signals = Reports(), Signals(proc)
        subject = enforcer(proc, reports, signals)

        subject.update(frozenset({"discord.desktop"}), grace_remaining=0.0)

        assert {pid for pid, _ in signals.sent} == {100, 101, 102}


class TestLaunchesDuringTheSession:
    def prepared(self, proc: FakeProc) -> tuple[AppEnforcer, Reports, Signals]:
        reports, signals = Reports(), Signals(proc)
        subject = enforcer(proc, reports, signals)
        subject.update(frozenset({"discord.desktop"}), grace_remaining=0.0)
        signals.sent.clear()
        reports.closed.clear()
        return subject, reports, signals

    def test_a_blocked_application_is_closed_on_launch(self, proc: FakeProc) -> None:
        subject, reports, signals = self.prepared(proc)
        proc.add(200, exe="/usr/bin/discord")

        subject.on_launch(200)

        assert (200, signal.SIGTERM) in signals.sent
        assert reports.closed == [(["Discord"], "launch")]

    def test_anything_else_is_left_alone(self, proc: FakeProc) -> None:
        subject, _reports, signals = self.prepared(proc)
        proc.add(200, exe="/usr/bin/firefox")

        subject.on_launch(200)

        assert signals.sent == []

    def test_a_process_that_has_already_gone_is_not_chased(self, proc: FakeProc) -> None:
        subject, _reports, signals = self.prepared(proc)

        subject.on_launch(9999)

        assert signals.sent == []

    def test_nothing_happens_when_no_session_blocks_applications(self, proc: FakeProc) -> None:
        reports, signals = Reports(), Signals(proc)
        subject = enforcer(proc, reports, signals)
        proc.add(200, exe="/usr/bin/discord")

        subject.on_launch(200)

        assert signals.sent == []


class TestResolvingWhatIsBlocked:
    def test_an_application_that_is_not_installed_is_skipped(self, proc: FakeProc) -> None:
        """A profile can name something uninstalled since; that is not an error."""
        proc.add(100, exe="/usr/bin/discord")
        reports, signals = Reports(), Signals(proc)
        subject = enforcer(proc, reports, signals)

        subject.update(frozenset({"discord.desktop", "gone.desktop"}), grace_remaining=0.0)

        assert [a.id for a in subject.apps] == ["discord.desktop"]
        assert (100, signal.SIGTERM) in signals.sent

    def test_the_catalogue_is_not_read_again_when_nothing_changed(self, proc: FakeProc) -> None:
        """The daemon polls every second, and discovery reads the filesystem."""
        reads = 0

        def catalogue() -> list[InstalledApp]:
            nonlocal reads
            reads += 1
            return [app()]

        reports, signals = Reports(), Signals(proc)
        subject = enforcer(proc, reports, signals, catalogue=catalogue)

        for _ in range(5):
            subject.update(frozenset({"discord.desktop"}), grace_remaining=0.0)

        assert reads == 1

    def test_an_application_added_mid_session_is_closed(self, proc: FakeProc) -> None:
        """The ratchet allows adding, so a new name must take effect at once."""
        proc.add(100, exe="/usr/bin/discord")
        proc.add(101, exe="/usr/lib/snapd/snap-confine", cgroup="0::/snap.spotify.spotify.scope")
        reports, signals = Reports(), Signals(proc)
        subject = enforcer(proc, reports, signals)

        subject.update(frozenset({"discord.desktop"}), grace_remaining=0.0)
        assert {pid for pid, _ in signals.sent} == {100}

        subject.update(frozenset({"discord.desktop", "spotify.desktop"}), grace_remaining=0.0)

        assert 101 in {pid for pid, _ in signals.sent}

    def test_emptying_the_list_forgets_the_applications(self, proc: FakeProc) -> None:
        """Between sessions a profile can lose an application entirely."""
        proc.add(100, exe="/usr/bin/discord")
        reports, signals = Reports(), Signals(proc)
        subject = enforcer(proc, reports, signals)
        subject.update(frozenset({"discord.desktop"}), grace_remaining=0.0)
        assert subject.apps

        subject.update(frozenset(), grace_remaining=0.0)

        assert subject.apps == []

    def test_blocking_nothing_closes_nothing(self, proc: FakeProc) -> None:
        proc.add(100, exe="/usr/bin/discord")
        reports, signals = Reports(), Signals(proc)
        subject = enforcer(proc, reports, signals)

        subject.update(frozenset(), grace_remaining=0.0)

        assert signals.sent == []
        assert subject.apps == []


class TestWhenTheSessionEnds:
    def test_launches_are_no_longer_touched(self, proc: FakeProc) -> None:
        proc.add(100, exe="/usr/bin/discord")
        reports, signals = Reports(), Signals(proc)
        subject = enforcer(proc, reports, signals)
        subject.update(frozenset({"discord.desktop"}), grace_remaining=0.0)
        signals.sent.clear()

        subject.stop()
        proc.add(200, exe="/usr/bin/discord")
        subject.on_launch(200)

        assert signals.sent == []
        assert not subject.enforcing

    def test_the_next_session_gets_its_own_grace(self, proc: FakeProc) -> None:
        """Otherwise the second session of the day would close things at once."""
        proc.add(100, exe="/usr/bin/discord")
        reports, signals = Reports(), Signals(proc)
        subject = enforcer(proc, reports, signals)
        subject.update(frozenset({"discord.desktop"}), grace_remaining=120.0)
        subject.stop()

        subject.update(frozenset({"discord.desktop"}), grace_remaining=120.0)

        assert len(reports.grace) == 2


class TestReportsThatFail:
    def test_a_failing_grace_report_does_not_stop_the_session(self, proc: FakeProc) -> None:
        proc.add(100, exe="/usr/bin/discord")
        reports, signals = Reports(), Signals(proc)
        subject = enforcer(proc, reports, signals)
        subject._on_grace = _explode  # noqa: SLF001

        subject.update(frozenset({"discord.desktop"}), grace_remaining=120.0)
        subject.update(frozenset({"discord.desktop"}), grace_remaining=0.0)

        assert (100, signal.SIGTERM) in signals.sent

    def test_a_failing_closure_report_does_not_stop_the_kill(self, proc: FakeProc) -> None:
        proc.add(100, exe="/usr/bin/discord")
        reports, signals = Reports(), Signals(proc)
        subject = enforcer(proc, reports, signals)
        subject._on_closed = _explode  # noqa: SLF001

        subject.update(frozenset({"discord.desktop"}), grace_remaining=0.0)

        assert (100, signal.SIGTERM) in signals.sent


def _explode(*args: object, **kwargs: object) -> None:
    raise RuntimeError("the engine is on fire")
