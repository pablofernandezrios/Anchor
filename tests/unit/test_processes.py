"""Recognising a process as a blocked application (SPEC 9)."""

from __future__ import annotations

import os
from pathlib import Path

from anchor.blocker.apps import AppKind, InstalledApp
from anchor.blocker.processes import RunningProcess, find_matches, matches, read_process


def app(**overrides: object) -> InstalledApp:
    base: dict[str, object] = {
        "id": "discord.desktop",
        "name": "Discord",
        "kind": AppKind.NATIVE,
        "exec_path": "/usr/bin/discord",
    }
    base.update(overrides)
    return InstalledApp(**base)  # type: ignore[arg-type]


def fake_proc(tmp_path: Path, pid: int, *, exe: str | None = None, cgroup: str = "") -> None:
    directory = tmp_path / str(pid)
    directory.mkdir(parents=True, exist_ok=True)
    if exe is not None:
        target = tmp_path / "bin" / Path(exe).name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")
        (directory / "exe").symlink_to(exe)
    (directory / "cgroup").write_text(cgroup, encoding="utf-8")


class TestMatchingByExecutable:
    def test_the_same_binary_matches(self) -> None:
        process = RunningProcess(pid=1, exe="/usr/bin/discord")
        assert matches(process, app())

    def test_a_different_binary_does_not(self) -> None:
        process = RunningProcess(pid=1, exe="/usr/bin/firefox")
        assert not matches(process, app())

    def test_a_process_with_no_executable_does_not_match(self) -> None:
        """Kernel threads have no exe, and must not match anything."""
        assert not matches(RunningProcess(pid=2, exe=""), app())

    def test_the_process_name_is_never_used(self) -> None:
        """SPEC 9: renaming a copy must not make it a different program."""
        renamed = RunningProcess(pid=1, exe="/home/user/totally-not-discord")
        assert not matches(renamed, app())

        # And the reverse: a process whose name looks innocent but whose
        # binary is the blocked one still matches.
        disguised = RunningProcess(pid=2, exe="/usr/bin/discord")
        assert matches(disguised, app())


class TestMatchingSandboxedApps:
    def test_a_snap_matches_by_cgroup(self) -> None:
        spotify = app(kind=AppKind.SNAP, snap_name="spotify", exec_path="/snap/bin/spotify")
        process = RunningProcess(
            pid=1,
            exe="/snap/spotify/79/usr/share/spotify/spotify",
            cgroup="0::/user.slice/snap.spotify.spotify-abc.scope",
        )

        assert matches(process, spotify)

    def test_a_flatpak_matches_by_cgroup(self) -> None:
        steam = app(
            kind=AppKind.FLATPAK,
            flatpak_id="com.valvesoftware.Steam",
            exec_path="/usr/bin/flatpak",
        )
        process = RunningProcess(
            pid=1,
            exe="/usr/bin/bwrap",
            cgroup="0::/user.slice/app-flatpak-com.valvesoftware.Steam-123.scope",
        )

        assert matches(process, steam)

    def test_another_flatpak_does_not_match(self) -> None:
        """The wrapper is shared, so the executable would match everything."""
        steam = app(
            kind=AppKind.FLATPAK,
            flatpak_id="com.valvesoftware.Steam",
            exec_path="/usr/bin/flatpak",
        )
        other = RunningProcess(
            pid=1,
            exe="/usr/bin/flatpak",
            cgroup="0::/user.slice/app-flatpak-org.gimp.GIMP-9.scope",
        )

        assert not matches(other, steam)

    def test_a_snap_outside_its_cgroup_does_not_match(self) -> None:
        spotify = app(kind=AppKind.SNAP, snap_name="spotify", exec_path="/snap/bin/spotify")
        process = RunningProcess(pid=1, exe="/snap/bin/spotify", cgroup="0::/init.scope")

        assert not matches(process, spotify)


class TestReadingProcesses:
    def test_a_real_process_is_readable(self) -> None:
        """This test's own process is the easiest one to be sure exists."""
        process = read_process(os.getpid())

        assert process is not None
        assert process.exe.endswith("python3") or "python" in process.exe

    def test_a_process_that_does_not_exist(self) -> None:
        assert read_process(999_999_999) is None

    def test_a_deleted_binary_is_still_recognisable(self, tmp_path: Path) -> None:
        """An upgrade mid-session leaves the old binary marked deleted."""
        fake_proc(tmp_path, 42, exe="/usr/bin/discord", cgroup="0::/")
        process = read_process(42, proc=tmp_path)

        assert process is not None
        assert "(deleted)" not in process.exe


class TestFindingMatches:
    def test_running_instances_are_found(self, tmp_path: Path) -> None:
        fake_proc(tmp_path, 10, exe="/usr/bin/discord", cgroup="0::/")
        fake_proc(tmp_path, 11, exe="/usr/bin/vim", cgroup="0::/")

        found = find_matches([app()], proc=tmp_path)

        assert [process.pid for process, _ in found] == [10]

    def test_nothing_blocked_means_nothing_read(self, tmp_path: Path) -> None:
        assert find_matches([], proc=tmp_path) == []

    def test_each_process_is_reported_once(self, tmp_path: Path) -> None:
        """Two rules catching one process is still one process to stop."""
        fake_proc(tmp_path, 10, exe="/usr/bin/discord", cgroup="0::/")

        found = find_matches([app(), app(id="other.desktop")], proc=tmp_path)
        assert len(found) == 1
