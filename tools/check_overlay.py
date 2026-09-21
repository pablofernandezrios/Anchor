#!/usr/bin/env python3
"""Prove the break overlay works on a real desktop (SPEC 10, ADR 2).

What the overlay says is decided away from GTK and tested here. What GTK does
with it cannot be: this machine has no display at all. So the questions go to
the machine that has one, and they are the questions ADR 2 left open.

    python3 tools/check_overlay.py
    python3 tools/check_overlay.py --seconds 60      # longer, to try things

Run it as yourself, inside the GNOME session. It needs no engine, no session
and no root: it drives the overlay directly with a made-up break, so nothing
is blocked, nothing is closed, and no session is started.

It covers your screen for half a minute. Ctrl-C in this terminal ends it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
if str(HERE.parent / "src") not in sys.path:  # run from a clone, uninstalled
    sys.path.insert(0, str(HERE.parent / "src"))

from anchor.agent.breakscreen import screen_for  # noqa: E402

BANNER = """
Anchor: does the break overlay work on this machine?
====================================================

Your screen is covered for about half a minute by a break screen counting
down. Nothing is blocked, nothing is closed, and no session is started.

While it is up, please try two things:
  1. Alt-Tab away from it, or click another window.
  2. If you have a second monitor, look at it.

Ctrl-C in this terminal ends it early.
"""


#: A break that is running, invented rather than asked of any engine.
def sample(seconds: float, *, hardness: str = "moderate") -> dict[str, Any]:
    return {
        "active": True,
        "profile": "Study",
        "level": "firm",
        "phase": "break",
        "remaining_seconds": 2 * 3600,
        "break": {
            "phase": "break",
            "remaining_seconds": seconds,
            "ends_at": time.time() + seconds,
            "long": False,
            "taken": 0,
            "postponed": 0,
            "skipped": 0,
            "type": "overlay",
            "hardness": hardness,
            "can_skip": hardness == "flexible",
            "can_postpone": hardness != "mandatory",
            "allow_sites": False,
        },
    }


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
        path = directory / "overlay.json"
        path.write_text(
            json.dumps(
                {
                    "check": "overlay",
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="check-results", help="Where to write the findings.")
    parser.add_argument("--yes", action="store_true", help="Do not stop to ask anything.")
    parser.add_argument(
        "--seconds", type=float, default=30.0, help="How long to keep the overlay up."
    )
    parser.add_argument(
        "--hardness",
        default="moderate",
        choices=("flexible", "moderate", "mandatory"),
        help="Which buttons the overlay should offer.",
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
        from gi.repository import GLib

        from anchor.agent.overlay import BreakOverlay, OverlayUnavailableError
    except ImportError as error:
        report.add("GTK 4", "fails", f"python3-gi or GTK 4 is missing ({error})")
        report.write(Path(args.out))
        return 1

    clicked: list[str] = []
    try:
        overlay = BreakOverlay(
            on_postpone=lambda: clicked.append("postpone"),
            on_skip=lambda: clicked.append("skip"),
        )
    except OverlayUnavailableError as error:
        report.add("GTK 4", "fails", str(error))
        report.write(Path(args.out))
        return 1
    except Exception as error:
        report.add(
            "GTK 4", "fails", f"the overlay could not be built: {error}", traceback.format_exc()
        )
        report.write(Path(args.out))
        return 1

    report.add("GTK 4", "works", "a display is open and GTK started")

    loop = GLib.MainLoop()
    remaining = [args.seconds]

    announced: list[str] = []

    def tick() -> bool:
        remaining[0] -= 1
        screen = screen_for(sample(remaining[0], hardness=args.hardness))
        if screen is None or remaining[0] <= 0:
            loop.quit()
            return False
        # A click is noted and the overlay stays up. In a real session it ends
        # the break; here it would end the check, and the question about
        # switching away — the one ADR 2 actually left open — would go with it.
        if clicked and not announced:
            announced.append(clicked[0])
            print(f"    {clicked[0]} reached the agent; the overlay stays up", flush=True)
        overlay.show(screen)
        return True

    def nudge() -> bool:
        """Ask for the one thing only a person can do, while there is time."""
        print(
            "\n    >>> now please try to Alt-Tab away, or click another window <<<",
            flush=True,
        )
        return False

    monitors = 0
    try:
        first = screen_for(sample(args.seconds, hardness=args.hardness))
        assert first is not None
        overlay.show(first)
        # Read now, while the windows exist: hide() destroys them, and asking
        # afterwards is how the first run came to say "0 monitor(s) found"
        # three lines under "1 found".
        monitors = overlay.monitors
        report.add(
            "the overlay is up",
            "works",
            f"one window per monitor: {monitors} found",
        )
        GLib.timeout_add(1000, tick)
        GLib.timeout_add(4000, nudge)
        loop.run()
    except KeyboardInterrupt:
        print("\nended early", flush=True)
    except Exception as error:
        report.add(
            "the countdown",
            "fails",
            f"the overlay broke while running: {error}",
            traceback.format_exc(),
        )
    finally:
        overlay.hide()

    if clicked:
        report.add("the buttons", "works", f"{clicked[0]} was clicked and reached the agent")

    ask(
        report,
        "covering the screen",
        "Did a dark break screen cover your whole screen, with BREAK, a "
        "countdown, and a line of advice? (yes / no / describe it)",
        assume_yes=args.yes,
    )
    ask(
        report,
        "every monitor",
        f"Anchor found {monitors} monitor(s). If you have more than "
        "one, was EVERY screen covered? (yes / only one / I have one monitor)",
        assume_yes=args.yes,
    )
    ask(
        report,
        "switching away",
        "You tried to Alt-Tab away or click another window. What happened? "
        "(it came back / it stayed away / I could not switch away at all)",
        assume_yes=args.yes,
    )
    ask(
        report,
        "the way out",
        f"Hardness was '{args.hardness}'. Were the buttons the ones you "
        "expected, and did the line underneath explain them? "
        "(describe what you saw)",
        assume_yes=args.yes,
    )
    ask(
        report,
        "afterwards",
        "Is your screen back to normal now, with no Anchor window left? "
        "(yes / something is still there)",
        assume_yes=args.yes,
    )

    path = report.write(Path(args.out))
    print(f"\nFindings written to {path}", flush=True)
    print("Send me that file, or paste what you see above.", flush=True)
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
