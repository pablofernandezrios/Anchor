#!/usr/bin/env python3
"""Prove the interface works on a real desktop (SPEC 14).

    python3 tools/check_gui.py

Run it as yourself, inside the GNOME session, with Anchor installed and
running. It opens the real interface against the real engine, asks you to
look at a few things, and writes what you saw to ``check-results/gui.json``.

Three of the rows cannot be answered anywhere else.

* **Do the icons draw?** An icon theme can carry a name and still fail to
  produce a picture, and what a user then sees is the "missing image" glyph.
  Every icon Anchor asks for is looked up here and actually rendered.
* **Does the window follow the system theme?** libadwaita is supposed to make
  that automatic. "Supposed to" is what a check is for.
* **Does the keyboard reach everything?** SPEC 14 asks for keyboard
  navigation and screen-reader labels, and only a person with a keyboard can
  say whether Tab actually gets anywhere.

It starts no session and blocks nothing. The one thing it can change is what
you change yourself while it is open.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE.parent / "src") not in sys.path:  # run from a clone, uninstalled
    sys.path.insert(0, str(HERE.parent / "src"))

from anchor.engine.paths import Paths  # noqa: E402

BANNER = """
Anchor: does the interface work on this machine?
================================================

The real window opens against the real engine. Nothing is blocked and no
session is started, unless you start one yourself.

Please look at:
  1. The six pages in the header: Home, Profiles, Schedules, Lists,
     Statistics, Settings. Click through all of them.
  2. The icons beside them. Any of them a grey "broken image" square?
  3. Press Tab a few times on any page. Does the focus ring move somewhere
     you can see?
  4. If you can, switch the system to dark mode while the window is open.

Close the window when you are done, and answer the questions here.
"""

#: Icons the interface uses that have no fallback of their own. The six in
#: the navigation bar are not here: each of those is a list, and the window
#: picks whichever one this theme can draw.
ICONS = (
    "view-list-symbolic",
    "media-playback-start-symbolic",
    "user-trash-symbolic",
    "list-add-symbolic",
    "list-remove-symbolic",
    "object-select-symbolic",
)


@dataclass
class Finding:
    name: str
    verdict: str
    detail: str
    evidence: str = ""

    def line(self) -> str:
        mark = {"works": "PASS", "fails": "FAIL", "unavailable": "SKIP", "observed": "LOOK"}
        return f"[{mark[self.verdict]}] {self.name}: {self.detail}"


@dataclass
class Report:
    machine: dict[str, str] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    unanswered: bool = False

    def add(self, name: str, verdict: str, detail: str, evidence: str = "") -> None:
        finding = Finding(name, verdict, detail, evidence)
        self.findings.append(finding)
        print(finding.line(), flush=True)
        for line in evidence.splitlines():
            print(f"        {line}", flush=True)

    @property
    def failed(self) -> bool:
        return any(finding.verdict == "fails" for finding in self.findings)

    def write(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "gui.json"
        path.write_text(
            json.dumps(
                {
                    "check": "gui",
                    "machine": self.machine,
                    "verdict": (
                        "fails" if self.failed else ("unanswered" if self.unanswered else "works")
                    ),
                    "findings": [asdict(finding) for finding in self.findings],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return path


def ask(report: Report, name: str, question: str, *, assume_yes: bool) -> None:
    """A row only the person watching can answer. Never a pass on its own."""
    if assume_yes:
        report.add(name, "observed", f"not asked: {question}")
        return
    answer = input(f"\n{question}\n> ").strip()
    report.add(name, "observed", answer or "(no answer)")


def check_icons(report: Report) -> None:
    """Every icon Anchor uses, looked up and drawn (SPEC 14)."""
    from gi.repository import Gdk, Gtk

    display = Gdk.Display.get_default()
    if display is None:
        report.add("icons", "unavailable", "no display to ask about icons")
        return

    theme = Gtk.IconTheme.get_for_display(display)
    missing: list[str] = []
    for name in ICONS:
        paintable = theme.lookup_icon(name, None, 32, 1, Gtk.TextDirection.NONE, 0)
        # `has_icon` is too generous — it says yes for names that then draw as
        # a broken square — and `get_file` is too strict, because an icon out
        # of the theme's cache has no file. What a lookup *resolved to* is the
        # honest answer: GTK renames it to "image-missing" when it gave up.
        if paintable is None or paintable.get_icon_name() == "image-missing":
            missing.append(name)

    if missing:
        report.add(
            "icons",
            "fails",
            f"{len(missing)} icon(s) would draw as a broken-image square",
            "\n".join(missing),
        )
        return
    report.add("icons", "works", f"all {len(ICONS)} icons draw on this theme")
    _report_chosen(report)


def _report_chosen(report: Report) -> None:
    """Which icon each page ended up with, since more than one was offered."""
    from anchor.gui.app import PAGES
    from anchor.gui.widgets import first_icon

    chosen = [f"{title}: {first_icon(*icons)}" for _name, title, icons in PAGES]
    report.add("navigation icons", "works", "the theme chose these", "\n".join(chosen))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="check-results", help="Where to write the findings.")
    parser.add_argument("--yes", action="store_true", help="Do not stop to ask anything.")
    parser.add_argument("--root", help="Talk to an engine on a relocated tree.")
    parser.add_argument(
        "--seconds",
        type=float,
        default=0.0,
        help="Close the window by itself after this long. For running unattended.",
    )
    args = parser.parse_args(argv)

    report = Report(
        machine={
            "session": os.environ.get("XDG_SESSION_TYPE", "none"),
            "desktop": os.environ.get("XDG_CURRENT_DESKTOP", "none"),
            "display": os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY") or "none",
        }
    )
    print(BANNER, flush=True)
    for key, value in report.machine.items():
        print(f"  {key}: {value}", flush=True)

    if report.machine["display"] == "none":
        report.add(
            "graphical session",
            "unavailable",
            "no WAYLAND_DISPLAY and no DISPLAY. Run this from a terminal inside "
            "the GNOME session, not over plain ssh",
        )
        report.unanswered = True
        path = report.write(Path(args.out))
        print(f"\nNothing was checked. Findings written to {path}", flush=True)
        return 0

    try:
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw, Gtk
    except (ImportError, ValueError) as error:
        report.add(
            "GTK 4 and libadwaita",
            "fails",
            f"missing ({error}). Install python3-gi, gir1.2-gtk-4.0 and gir1.2-adw-1",
        )
        report.write(Path(args.out))
        return 1

    report.add(
        "GTK 4 and libadwaita",
        "works",
        f"GTK {Gtk.get_major_version()}.{Gtk.get_minor_version()}, "
        f"libadwaita {Adw.MAJOR_VERSION}.{Adw.MINOR_VERSION}",
    )

    paths = Paths.resolve(args.root)
    if not paths.engine_socket.exists():
        report.add(
            "the engine",
            "unavailable",
            f"{paths.engine_socket} is not there. The window will open and say so, "
            "which is worth seeing too, but nothing will be in it",
        )
        report.unanswered = True
    else:
        report.add("the engine", "works", f"answering on {paths.engine_socket}")

    try:
        from anchor.gui.app import AnchorApplication

        application = AnchorApplication(paths.engine_socket)
    except Exception as error:
        report.add(
            "the interface",
            "fails",
            f"it could not be built: {error}",
            traceback.format_exc(),
        )
        report.write(Path(args.out))
        return 1

    # Icons need a display connection, which the application has once it runs;
    # asking beforehand answers about a theme nothing has loaded.
    application.connect("activate", lambda *_: check_icons(report))

    if args.seconds > 0:
        from gi.repository import GLib

        def close() -> bool:
            application.quit()
            return False

        application.connect(
            "activate", lambda *_: GLib.timeout_add(int(args.seconds * 1000), close)
        )
        print(f"\nOpening the window for {args.seconds:.0f} s.\n", flush=True)
    else:
        print("\nOpening the window. Close it when you have looked.\n", flush=True)
    code = application.run([])
    report.add(
        "the window",
        "works" if code == 0 else "fails",
        f"it opened and closed with exit code {code}",
    )

    for name, question in (
        ("pages", "Did all six pages open, with something on each? (yes / what was wrong)"),
        ("icons seen", "Were any icons a grey broken-image square? (no / which ones)"),
        ("keyboard", "Did Tab move a visible focus ring around the page? (yes / no)"),
        ("theme", "If you switched to dark mode, did the window follow? (yes / no / not tried)"),
        ("legibility", "Anything unreadable, cut off, or overlapping? (no / what)"),
    ):
        ask(report, name, question, assume_yes=args.yes)

    path = report.write(Path(args.out))
    print(f"\nFindings written to {path}", flush=True)
    print("Send me that file, or paste the lines above.", flush=True)
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
