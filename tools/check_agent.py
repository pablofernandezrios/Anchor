#!/usr/bin/env python3
"""Prove the indicator and the notifications work on a real desktop (SPEC 14.1).

Everything the agent decides is tested without a screen. What cannot be is
whether GNOME actually draws it: whether the item reaches the top bar, whether
the label appears beside the icon rather than the icon alone, and whether the
notifications look like something worth reading. That needs eyes.

    python3 tools/check_agent.py

Run it as yourself, inside the GNOME session, NOT over ssh and NOT with sudo:
it needs your session bus. It needs no engine, no session and no root — it
drives the indicator itself with made-up numbers, so nothing is blocked and
nothing is closed.

It puts an item in your top bar for about half a minute and shows three
notifications. That is all it touches.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
if str(HERE.parent / "src") not in sys.path:  # run from a clone, uninstalled
    sys.path.insert(0, str(HERE.parent / "src"))

from anchor.agent.indicator import (  # noqa: E402
    IndicatorModel,
    IndicatorView,
)
from anchor.agent.notifications import plan  # noqa: E402

BANNER = """
Anchor: does the indicator reach the top bar?
=============================================

For about half a minute an Anchor item appears in your top bar, counting down,
and three notifications arrive. Nothing is blocked, nothing is closed, and no
session is started.
"""

HOUR = 3600

#: The numbers the item counts down through, so the label visibly changes
#: rather than sitting still while you look at it.
COUNTDOWN = (2 * HOUR + 14 * 60, 61 * 60, 59 * 60, 60, 0)

SAMPLE_STATUS: dict[str, Any] = {
    "active": True,
    "profile": "Study",
    "level": "firm",
    "phase": "working",
    "started_at": 0.0,
    "ends_at": 0.0,
    "remaining_seconds": COUNTDOWN[0],
    "blocked_attempts": 3,
    "app_blocks": 1,
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
    """Set when the questions could not be asked here at all.

    Not a failure, and not a pass either. A check run over ssh answers
    nothing, and reporting "works" because nothing went wrong would be the
    same self-grading mistake Milestone 0 made three times.
    """

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
        path = directory / "agent.json"
        path.write_text(
            json.dumps(
                {
                    "check": "agent",
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


def machine_facts() -> dict[str, str]:
    return {
        "session": os.environ.get("XDG_SESSION_TYPE", "none"),
        "desktop": os.environ.get("XDG_CURRENT_DESKTOP", "none"),
        "bus": "yes" if os.environ.get("DBUS_SESSION_BUS_ADDRESS") else "no",
    }


def preflight(report: Report) -> bool:
    """Reasons this check cannot answer its questions here."""
    facts = report.machine
    if facts["bus"] == "no":
        report.add(
            "session bus",
            "unavailable",
            "DBUS_SESSION_BUS_ADDRESS is unset. Run this from a terminal inside "
            "the GNOME session, not over plain ssh and not as root",
        )
        return False
    if facts["session"] not in ("wayland", "x11"):
        report.add(
            "graphical session",
            "unavailable",
            f"XDG_SESSION_TYPE is {facts['session']}, so there is no top bar to look at",
        )
        return False
    report.add(
        "graphical session",
        "works",
        f"{facts['desktop']} on {facts['session']}",
    )
    return True


def ask(report: Report, name: str, question: str, *, assume_yes: bool) -> None:
    """A row only the person watching the screen can answer.

    Never a pass on its own. A check that grades itself here reports whatever
    it was told to report, which is how Milestone 0 produced three confident
    wrong answers before this rule was written down.
    """
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
        "--seconds", type=float, default=6.0, help="How long to hold each countdown step."
    )
    args = parser.parse_args(argv)

    report = Report(machine=machine_facts())
    print(BANNER, flush=True)
    for key, value in report.machine.items():
        print(f"  {key}: {value}", flush=True)

    if not preflight(report):
        report.unanswered = True
        path = report.write(Path(args.out))
        print(f"\nNothing was checked. Findings written to {path}", flush=True)
        # Not a failure: it is an answer about where the question can be asked.
        return 0

    try:
        from gi.repository import GLib

        from anchor.agent.desktop import (
            DesktopNotifier,
            DesktopUnavailableError,
            TrayItem,
            session_bus,
        )
    except ImportError as error:
        report.add(
            "PyGObject",
            "fails",
            f"python3-gi is not installed ({error}), so the agent cannot draw anything",
        )
        report.write(Path(args.out))
        return 1

    try:
        connection = session_bus()
    except DesktopUnavailableError as error:
        report.add("session bus", "fails", str(error))
        report.write(Path(args.out))
        return 1
    report.add("session bus", "works", "connected")

    watcher = _watcher_state(connection, GLib)
    report.add(*watcher)

    clicked: list[bool] = []
    try:
        tray = TrayItem(connection)
        notifier = DesktopNotifier(connection)
        model = IndicatorModel()
        tray.start(on_activate=lambda: clicked.append(True))
    except Exception as error:
        # A traceback would tell me what broke and tell the owner nothing.
        # This is the same lesson as the Milestone 0 probes: a check that
        # cannot report its own failure is a check that wastes someone's time.
        report.add(
            "the item was exported",
            "fails",
            f"the agent could not put an item on the bus: {error}",
            traceback.format_exc(),
        )
        path = report.write(Path(args.out))
        print(f"\nFindings written to {path}. Send me that file.", flush=True)
        return 1
    report.add("the item was exported", "works", "org.kde.StatusNotifierItem is on the bus")

    loop = GLib.MainLoop()
    steps = list(COUNTDOWN)

    def tick() -> bool:
        if not steps:
            loop.quit()
            return False
        remaining = steps.pop(0)
        status = dict(SAMPLE_STATUS, remaining_seconds=remaining)
        model.update_status(status)
        view: IndicatorView = model.view
        tray.show(view)
        print(f"    top bar should read {view.label}", flush=True)
        return True

    def notify() -> bool:
        for event, payload in (
            ("blocked.attempt", {"domain": "youtube.com", "rule": "youtube.com"}),
            ("apps.grace", {"apps": ["Discord", "Slack"], "seconds": 120}),
            ("apps.closed", {"apps": ["Discord"], "reason": "launch"}),
        ):
            notification = plan(event, payload)
            if notification is not None:
                notifier.send(notification)
                print(f"    sent: {notification.summary}", flush=True)
        return False

    try:
        tick()
        GLib.timeout_add(int(args.seconds * 1000), tick)
        GLib.timeout_add(1500, notify)
        loop.run()

        # Hide it the way the end of a session does, and leave nothing behind.
        model.update_status({"active": False})
        tray.show(model.view)
    except Exception as error:
        report.add(
            "the countdown",
            "fails",
            f"the indicator broke while updating: {error}",
            traceback.format_exc(),
        )
    finally:
        tray.stop()

    ask(
        report,
        "the icon",
        "Did an Anchor icon appear in the top bar? (yes / no)",
        assume_yes=args.yes,
    )
    ask(
        report,
        "the label",
        "Did the time appear BESIDE the icon, and count down "
        "(2:14 -> 1:01 -> 0:59 -> 0:01 -> 0:00)? "
        "(yes / icon only / something else)",
        assume_yes=args.yes,
    )
    ask(
        report,
        "the notifications",
        "Three notifications: a blocked site, a two-minute warning, and a "
        "closed application. Did all three appear, and did the warning stay "
        "on screen rather than fading? (describe what you saw)",
        assume_yes=args.yes,
    )
    ask(
        report,
        "the item left",
        "Is the top bar clear again now? (yes / the icon is still there)",
        assume_yes=args.yes,
    )
    if clicked:
        report.add("clicking it", "works", "the Activate call reached the agent")

    path = report.write(Path(args.out))
    print(f"\nFindings written to {path}", flush=True)
    print("Send me that file, or paste what you see above.", flush=True)
    return 1 if report.failed else 0


def _watcher_state(connection: Any, glib: Any) -> tuple[str, str, str]:
    """Whether anything is listening for indicators at all (SPEC 14.1)."""
    try:
        reply = connection.call_sync(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "NameHasOwner",
            glib.Variant("(s)", ("org.kde.StatusNotifierWatcher",)),
            glib.VariantType("(b)"),
            0,
            2000,
            None,
        )
    except glib.Error as error:
        return ("StatusNotifierWatcher", "fails", f"could not ask the bus: {error.message}")

    if reply.unpack()[0]:
        return (
            "StatusNotifierWatcher",
            "works",
            "something is listening for indicators, so an item will be shown",
        )
    return (
        "StatusNotifierWatcher",
        "fails",
        "nothing is listening for indicators. On Ubuntu that means "
        "gnome-shell-extension-appindicator is missing or disabled; SPEC 14.1 "
        "says onboarding detects this and explains it",
    )


if __name__ == "__main__":
    sys.exit(main())
