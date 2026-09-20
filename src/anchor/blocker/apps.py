"""Finding the applications installed on this machine (SPEC 9).

The interface shows a list of installed applications and the user ticks the
ones to block, so Anchor has to produce that list the way the desktop itself
does: from ``.desktop`` entries, including the ones Snap and Flatpak export.

Two things make this less mechanical than it sounds.

**The command in an entry is not a path.** ``Exec`` is a command line with
placeholders, sometimes prefixed with ``env VAR=...``, and for a Flatpak it is
``flatpak run`` followed by an application id. What matters is what ends up
running, which is what a process can later be matched against.

**Packaging decides how a process is recognised.** A native program is its
executable; a Snap or a Flatpak is better recognised by its cgroup, because
the binary is a wrapper shared by every application in that format. SPEC 9 is
explicit that nothing is ever matched by process name.
"""

from __future__ import annotations

import configparser
import logging
import os
import shlex
import shutil
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

log = logging.getLogger("anchor-blockerd")

#: Directories holding ``.desktop`` entries, relative to a root.
SYSTEM_DIRS: Final = (
    "usr/share/applications",
    "usr/local/share/applications",
    # Snap exports its entries here, and Flatpak exports system-wide ones here.
    "var/lib/snapd/desktop/applications",
    "var/lib/flatpak/exports/share/applications",
)

#: The same, relative to the user's home directory.
USER_DIRS: Final = (
    ".local/share/applications",
    ".local/share/flatpak/exports/share/applications",
)

#: Placeholders a desktop entry may put in its command (freedesktop spec).
_FIELD_CODES: Final = frozenset(
    {"%f", "%F", "%u", "%U", "%d", "%D", "%n", "%N", "%i", "%c", "%k", "%v", "%m", "@@", "@@u"}
)


class AppKind(StrEnum):
    """How an application is packaged, which decides how it is recognised."""

    NATIVE = "native"
    SNAP = "snap"
    FLATPAK = "flatpak"


@dataclass(frozen=True, slots=True)
class InstalledApp:
    """One application the user could choose to block."""

    id: str
    """The desktop entry's file name, which is stable across upgrades."""

    name: str
    kind: AppKind
    exec_path: str
    icon: str = ""
    desktop_file: Path | None = None
    snap_name: str = ""
    flatpak_id: str = ""

    @property
    def cgroup_hint(self) -> str:
        """The fragment that identifies this app in a process's cgroup.

        Empty for a native application, which is matched by its executable
        instead.
        """
        if self.kind is AppKind.SNAP and self.snap_name:
            return f"snap.{self.snap_name}."
        if self.kind is AppKind.FLATPAK and self.flatpak_id:
            return f"app-flatpak-{self.flatpak_id}-"
        return ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": str(self.kind),
            "exec_path": self.exec_path,
            "icon": self.icon,
            "snap_name": self.snap_name,
            "flatpak_id": self.flatpak_id,
        }


def _command_tokens(exec_line: str) -> list[str]:
    """Split an ``Exec`` line, dropping the placeholders."""
    try:
        tokens = shlex.split(exec_line)
    except ValueError:
        tokens = exec_line.split()
    return [token for token in tokens if token not in _FIELD_CODES and "%" not in token]


def _program(tokens: list[str]) -> tuple[str, list[str]]:
    """Return the program being run, and whatever follows it.

    Skips an ``env`` prefix and its assignments, which Snap entries use, so the
    result is the thing that actually executes rather than the tool that sets
    up its environment.
    """
    rest = list(tokens)
    while rest:
        head = rest[0]
        if head in ("env", "/usr/bin/env"):
            rest = rest[1:]
            while rest and "=" in rest[0] and not rest[0].startswith("/"):
                rest = rest[1:]
            continue
        return head, rest[1:]
    return "", []


def _resolve(program: str) -> str:
    """Turn a command into the absolute path that will run.

    Desktop entries often say ``vim`` rather than ``/usr/bin/vim``, and a
    relative name cannot be compared against a running process's executable.
    An entry Anchor cannot resolve would silently never match, so it is
    resolved here, once, rather than hopefully later.
    """
    if not program:
        return ""
    if program.startswith("/"):
        return program

    found = shutil.which(program)
    if found:
        return found

    log.debug("could not resolve %r to a path; it will only match by cgroup", program)
    return program


def _flatpak_id(program: str, arguments: list[str]) -> str:
    """The application id from a ``flatpak run ...`` command."""
    if Path(program).name != "flatpak":
        return ""
    for argument in arguments:
        if argument != "run" and not argument.startswith("-"):
            return argument
    return ""


def parse_desktop_entry(path: Path) -> InstalledApp | None:
    """Read one ``.desktop`` file, or ``None`` if it is not a launchable app.

    Anything unreadable or malformed costs that one entry rather than the whole
    listing: a single broken file on a machine should not hide every other
    application from the interface.
    """
    parser = configparser.RawConfigParser()
    # Desktop entries are case-sensitive and full of % characters, so neither
    # key folding nor interpolation belongs here.
    parser.optionxform = str  # type: ignore[method-assign,assignment]

    try:
        with path.open(encoding="utf-8") as handle:
            parser.read_file(handle)
    except (OSError, configparser.Error, UnicodeDecodeError) as error:
        log.debug("skipping %s: %s", path, error)
        return None

    if not parser.has_section("Desktop Entry"):
        return None
    entry = parser["Desktop Entry"]

    if entry.get("Type", "Application") != "Application":
        return None
    if entry.get("NoDisplay", "false").lower() == "true":
        return None
    if entry.get("Hidden", "false").lower() == "true":
        return None

    exec_line = entry.get("Exec", "").strip()
    if not exec_line:
        return None

    program, arguments = _program(_command_tokens(exec_line))
    if not program:
        return None
    resolved = _resolve(program)

    flatpak = _flatpak_id(program, arguments) or entry.get("X-Flatpak", "").strip()
    snap_name = ""
    kind = AppKind.NATIVE

    if flatpak:
        kind = AppKind.FLATPAK
    elif resolved.startswith("/snap/") or "/snapd/" in str(path):
        kind = AppKind.SNAP
        # /snap/bin/spotify, or the entry's own name: spotify_spotify.desktop.
        snap_name = (
            Path(program).name if program.startswith("/snap/bin/") else path.name.split("_", 1)[0]
        )

    return InstalledApp(
        id=path.name,
        name=entry.get("Name", path.stem).strip(),
        kind=kind,
        exec_path=resolved,
        icon=entry.get("Icon", "").strip(),
        desktop_file=path,
        snap_name=snap_name,
        flatpak_id=flatpak,
    )


def discover(*, roots: list[Path] | None = None, home: Path | None = None) -> list[InstalledApp]:
    """List every application installed on this machine (SPEC 9).

    Entries are searched in XDG order and keyed by file name, so a user's own
    copy in ``~/.local`` replaces the system one rather than appearing twice.
    """
    search: list[Path] = []
    for root in roots if roots is not None else [Path("/")]:
        search.extend(root / directory for directory in SYSTEM_DIRS)

    home_dir = home if home is not None else Path(os.path.expanduser("~"))  # noqa: PTH111
    search.extend(home_dir / directory for directory in USER_DIRS)

    # Later directories win, which is why the user's own come last.
    found: dict[str, InstalledApp] = {}
    for directory in search:
        try:
            entries = sorted(directory.glob("*.desktop"))
        except OSError:
            continue
        for entry in entries:
            app = parse_desktop_entry(entry)
            if app is not None:
                found[app.id] = app

    return sorted(found.values(), key=lambda app: (app.name.lower(), app.id))
