#!/usr/bin/env python3
"""Run the Milestone 0 spikes that need a real GNOME session.

The other four spikes run unattended on a CI virtual machine. These three need
a desktop: a top-bar indicator, a fullscreen overlay across monitors, and the
browser policy paths. Run this inside the graphical session of a disposable
virtual machine, never on a machine you care about.

    python3 spikes/run_desktop.py                 # look, do not touch
    sudo python3 spikes/run_desktop.py --write    # also write browser policies

Everything it writes is restored before it exits, but take a snapshot first
anyway: that is what a snapshot is for.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DESKTOP_SPIKES = ("s4_indicator.py", "s5_wayland_overlay.py", "s6_browser_doh.py")

BANNER = """
Anchor: Milestone 0 desktop spikes
==================================

These open windows on your screen and read your browser configuration.
Two of them want you to watch: an indicator should appear in the top bar, and
a black overlay should cover every monitor for five seconds.

Run this in a disposable virtual machine with a fresh snapshot.
"""


def preflight() -> list[str]:
    """Reasons these spikes cannot answer their questions here.

    Two of the three need to draw on a screen. Running them from a console or
    over plain ssh produces a page of skips that look like results, so it is
    better to say so before anything runs.
    """
    problems: list[str] = []

    session = os.environ.get("XDG_SESSION_TYPE", "")
    if session not in ("wayland", "x11"):
        problems.append(
            f"XDG_SESSION_TYPE is {session or 'unset'}, not wayland or x11. "
            "Open a terminal inside the GNOME session rather than a console or ssh."
        )
    if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")):
        problems.append("Neither WAYLAND_DISPLAY nor DISPLAY is set, so no window can be opened.")
    if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        problems.append("DBUS_SESSION_BUS_ADDRESS is unset, so the session bus is unreachable.")

    return problems


def main(argv: list[str]) -> int:
    out_dir = Path(argv[0]) if argv and not argv[0].startswith("-") else HERE / "results"
    write = "--write" in argv
    out_dir.mkdir(parents=True, exist_ok=True)

    print(BANNER)

    problems = preflight()
    if problems:
        print("This session cannot answer two of the three questions:\n")
        for problem in problems:
            print(f"  - {problem}")
        print(
            "\nThe browser policy spike works anywhere, so you can continue and "
            "get that one answer, but the indicator and overlay results will be "
            "skips rather than findings.\n"
        )
        if input("Continue anyway? Type yes: ").strip().lower() not in ("yes", "y"):
            print("Nothing was run. Open a terminal inside the GNOME session and try again.")
            return 1
        print()
    if write:
        print("Running with --write: browser policy files will be written and restored.\n")
    if input("Type yes to continue: ").strip().lower() not in ("yes", "y"):
        print("Nothing was run.")
        return 1

    failures = 0
    for name in DESKTOP_SPIKES:
        print(f"\n{'=' * 70}\n{name}\n{'=' * 70}")
        command = [sys.executable, str(HERE / name), str(out_dir)]
        if write:
            command.append("--write")
        result = subprocess.run(command, check=False, cwd=HERE)
        failures += 1 if result.returncode else 0

    print(f"\n{'=' * 70}")
    summary = subprocess.run(
        [sys.executable, str(HERE / "summarise.py"), str(out_dir)],
        check=False,
        capture_output=True,
        text=True,
    )
    print(summary.stdout)

    combined = out_dir / "desktop-findings.md"
    combined.write_text(summary.stdout, encoding="utf-8")
    print(f"Paste this file back to the engineering thread:\n  {combined}\n")

    if failures:
        print(f"{failures} spike(s) contradicted the specification. That is the useful result.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
