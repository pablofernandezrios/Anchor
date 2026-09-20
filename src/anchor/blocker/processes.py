"""Recognising a running process as a blocked application (SPEC 9).

Two things identify a process, and the specification is explicit that the
obvious third is not one of them.

**Its executable.** ``/proc/<pid>/exe`` is a symbolic link to the binary that
is actually running. Both sides are resolved before comparing, because
``/usr/bin/vim`` is often a link to ``/usr/bin/vim.basic`` and a desktop entry
names the former.

**Its cgroup.** A Snap runs under ``snap.<name>.*`` and a Flatpak under
``app-flatpak-<id>-*``. This matters because the executable of a sandboxed
application is a wrapper shared by every application in that format: matching
Spotify by ``/snap/bin/spotify`` would work, but matching a Flatpak by
``/usr/bin/flatpak`` would match every Flatpak on the machine.

**Never the process name.** SPEC 9 rules it out, and rightly: a name is
whatever the program says it is, it is truncated to fifteen characters in
``/proc``, and anyone wanting to dodge a block can copy a binary and rename it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from anchor.blocker.apps import InstalledApp

log = logging.getLogger("anchor-blockerd")

PROC = Path("/proc")


@dataclass(frozen=True, slots=True)
class RunningProcess:
    """What Anchor knows about one running process."""

    pid: int
    exe: str = ""
    """The resolved executable, empty when it could not be read."""

    cgroup: str = ""


def _resolve(path: str) -> str:
    """Follow links, so two names for one binary compare equal."""
    if not path:
        return ""
    try:
        return str(Path(path).resolve())
    except OSError:
        return path


def read_process(pid: int, *, proc: Path = PROC) -> RunningProcess | None:
    """Read what identifies a process, or ``None`` if it is gone.

    A process exiting between being noticed and being read is the normal case,
    not an error: short-lived processes are most of what a machine does.
    """
    directory = proc / str(pid)
    try:
        exe = str((directory / "exe").readlink())
    except OSError:
        # Either it exited, or it belongs to another user and this is not root.
        exe = ""

    try:
        cgroup = (directory / "cgroup").read_text(encoding="utf-8", errors="replace")
    except OSError:
        cgroup = ""

    if not exe and not cgroup:
        return None

    # A deleted binary reads as "/path/to/thing (deleted)".
    exe = exe.removesuffix(" (deleted)")
    return RunningProcess(pid=pid, exe=_resolve(exe), cgroup=cgroup)


def matches(process: RunningProcess, app: InstalledApp) -> bool:
    """Whether ``process`` is an instance of ``app``."""
    hint = app.cgroup_hint
    if hint and hint in process.cgroup:
        return True

    # A sandboxed application is identified by its cgroup alone. Falling back
    # to the executable would match every Flatpak through the shared wrapper.
    if hint:
        return False

    return bool(process.exe) and process.exe == _resolve(app.exec_path)


def find_matches(
    apps: list[InstalledApp], *, proc: Path = PROC
) -> list[tuple[RunningProcess, InstalledApp]]:
    """Every running process that is one of ``apps``."""
    if not apps:
        return []

    found: list[tuple[RunningProcess, InstalledApp]] = []
    for entry in _pids(proc):
        process = read_process(entry, proc=proc)
        if process is None:
            continue
        for app in apps:
            if matches(process, app):
                found.append((process, app))
                break
    return found


def _pids(proc: Path) -> list[int]:
    try:
        return sorted(int(entry.name) for entry in proc.iterdir() if entry.name.isdigit())
    except OSError as error:
        log.warning("could not read %s: %s", proc, error)
        return []
