#!/usr/bin/env python3
"""Prove application blocking works on a real desktop (SPEC 7.1, 9).

The automated tests close copies of `/usr/bin/sleep`, which is honest as far as
it goes and goes no further: nothing in this container is a Snap, a Flatpak or
a GTK application with fifteen helper processes. This script asks the questions
those tests cannot, on the machine where the answers matter.

    python3 tools/check_app_blocking.py --list
    python3 tools/check_app_blocking.py --app org.telegram.desktop.desktop

Run it as yourself, NOT with sudo: it only signals your own processes, and
running it as root would look for desktop entries in root's home rather than
yours. It touches no DNS, no firewall rules and no systemd units — only the
application you name.

It will close that application. Save your work first, and take a VM snapshot,
which is what snapshots are for.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
if str(HERE.parent / "src") not in sys.path:  # run from a clone, uninstalled
    sys.path.insert(0, str(HERE.parent / "src"))

from anchor.blocker.apps import AppKind, InstalledApp, discover  # noqa: E402
from anchor.blocker.enforcement import escalate, is_alive  # noqa: E402
from anchor.blocker.processes import find_matches  # noqa: E402
from anchor.blocker.watcher import ProcessWatcher, wait_for  # noqa: E402

BANNER = """
Anchor: does application blocking work on this machine?
=======================================================

This closes the application you name, twice: once the way a session start
does, and once the way a launch during a session does. Anything unsaved in it
is lost.

It changes no DNS, no firewall rules and no systemd units.
"""


@dataclass
class Finding:
    name: str
    verdict: str
    detail: str
    evidence: str = ""

    def line(self) -> str:
        mark = {
            "works": "PASS",
            "fails": "FAIL",
            "unavailable": "SKIP",
            "observed": "LOOK",
        }[self.verdict]
        return f"[{mark}] {self.name}: {self.detail}"


@dataclass
class Report:
    machine: dict[str, str]
    findings: list[Finding] = field(default_factory=list)

    def add(self, name: str, verdict: str, detail: str, evidence: str = "") -> Finding:
        finding = Finding(name, verdict, detail, evidence)
        self.findings.append(finding)
        print(finding.line(), flush=True)
        if evidence:
            for line in evidence.splitlines():
                print(f"        {line}", flush=True)
        return finding

    @property
    def failed(self) -> bool:
        return any(finding.verdict == "fails" for finding in self.findings)

    def write(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "app-blocking.json"
        path.write_text(
            json.dumps(
                {
                    "check": "app-blocking",
                    "machine": self.machine,
                    "verdict": "fails" if self.failed else "works",
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
        "kernel": os.uname().release,
        "session": os.environ.get("XDG_SESSION_TYPE", "none"),
        "desktop": os.environ.get("XDG_CURRENT_DESKTOP", "none"),
        "user": str(os.getuid()),
    }


def describe(app: InstalledApp) -> str:
    hint = app.cgroup_hint or "(matched by executable)"
    return "\n".join(
        [
            app.id,
            f"  name:   {app.name}",
            f"  kind:   {app.kind}",
            f"  exec:   {app.exec_path}",
            f"  cgroup: {hint}",
        ]
    )


def show_processes(matches: list[tuple[Any, InstalledApp]]) -> str:
    lines = []
    for process, app in matches:
        why = "cgroup" if app.cgroup_hint and app.cgroup_hint in process.cgroup else "executable"
        lines.append(f"pid {process.pid} matched by {why}: {process.exe or '(unreadable)'}")
    return "\n".join(lines)


def ask(prompt: str, *, assume_yes: bool) -> None:
    if assume_yes:
        print(f"{prompt} (assuming yes)", flush=True)
        return
    input(f"{prompt} ")


def observed(report: Report, question: str, *, assume_yes: bool) -> None:
    """A row only a person watching the screen can answer.

    It is never a pass on its own. A check that grades itself here would
    report whatever it was told to report.
    """
    if assume_yes:
        report.add("what you saw", "observed", f"not asked: {question}")
        return
    answer = input(f"{question} ").strip()
    report.add("what you saw", "observed", answer or "(no answer)")


# -- the demo application -------------------------------------------------


def make_demo_app(directory: Path) -> InstalledApp:
    """A stand-in application, so the script can be checked without a desktop."""
    source = shutil.which("sleep")
    if not source:
        raise SystemExit("the demo needs /usr/bin/sleep")
    binary = directory / "anchor-demo-app"
    shutil.copy(source, binary)
    return InstalledApp(
        id="anchor-demo.desktop",
        name="Anchor demo application",
        kind=AppKind.NATIVE,
        exec_path=str(binary),
    )


# -- the check ------------------------------------------------------------


def check(app: InstalledApp, report: Report, args: argparse.Namespace) -> None:
    launched: list[subprocess.Popen[bytes]] = []

    def launch_demo() -> None:
        if args.demo:
            launched.append(
                subprocess.Popen(
                    [app.exec_path, "600"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            )

    # 1. Can Anchor see the launch at all?
    seen: list[int] = []
    watcher = ProcessWatcher(seen.append)
    watcher.start()
    try:
        if watcher.using_events:
            report.add(
                "kernel process events",
                "works",
                "the netlink connector is subscribed; launches arrive in about a millisecond",
            )
        else:
            report.add(
                "kernel process events",
                "unavailable",
                "no netlink connector here, so launches are noticed by the two-second poll "
                "(SPEC 9's fallback). Blocking still works, up to two seconds later",
            )

        # 2. Is the running application recognised?
        print(f"\nLaunch {app.name} now, and wait for its window.", flush=True)
        ask("Press Enter once it is open.", assume_yes=args.yes)
        launch_demo()

        matches = find_matches([app])
        if not wait_for(lambda: bool(find_matches([app])), timeout=10):
            report.add(
                "identification",
                "fails",
                f"nothing running matches {app.id}",
                "Anchor would not block it. Run with --list to check the identifier, and "
                "send me this file: the executable or cgroup in the desktop entry does not "
                "match what the program actually runs as.",
            )
            return
        matches = find_matches([app])
        report.add(
            "identification",
            "works",
            f"{len(matches)} process(es) recognised as {app.name}",
            show_processes(matches),
        )

        # 3. The two minutes (SPEC 7.1), shortened so this takes less of your day.
        pids = [process.pid for process, _ in matches]
        print(
            f"\nThe grace period starts now: {args.grace} s (a real session gives 120).", flush=True
        )
        time.sleep(args.grace)
        still_there = [pid for pid in pids if is_alive(pid)]
        if still_there:
            report.add(
                "grace period",
                "works",
                f"{len(still_there)} process(es) were left alone for {args.grace} s",
            )
        else:
            report.add(
                "grace period",
                "fails",
                "the application went away during the grace, so this proves nothing about it",
                "If you closed it yourself, run the check again.",
            )
            return

        # 4. Closing it the way a session start does.
        started = time.monotonic()
        killed = escalate(still_there, kill_after=10.0)
        elapsed = time.monotonic() - started
        gone = wait_for(lambda: not any(is_alive(pid) for pid in still_there), timeout=5)
        report.add(
            "closing what was already open",
            "works" if gone else "fails",
            (
                f"closed in {elapsed:.1f} s with "
                + ("SIGKILL after SIGTERM was ignored" if killed else "SIGTERM alone")
                if gone
                else "the processes are still alive after SIGTERM and SIGKILL"
            ),
            "" if gone else "This is worth telling me about: something is refusing SIGKILL.",
        )
        if not gone:
            return
        observed(
            report, "Did its window disappear? (yes/no, plus anything odd)", assume_yes=args.yes
        )

        # 5. Closing it the way a launch during a session does.
        seen.clear()
        print(f"\nNow launch {app.name} again. Anchor should close it as it opens.", flush=True)
        ask("Press Enter, then launch it.", assume_yes=args.yes)
        launch_demo()

        noticed = wait_for(lambda: bool(find_matches([app])), timeout=15)
        if not noticed:
            report.add("closing a launch", "fails", "it never came back; nothing to close")
            return

        detected = time.monotonic()
        again = [process.pid for process, _ in find_matches([app])]
        escalate(again, kill_after=10.0)
        dead = wait_for(lambda: not any(is_alive(pid) for pid in again), timeout=10)
        report.add(
            "closing a launch",
            "works" if dead else "fails",
            f"closed {time.monotonic() - detected:.2f} s after Anchor saw it"
            if dead
            else "it survived",
        )
        observed(
            report,
            "How much of the window did you see before it went?"
            " (nothing / a flash / it stayed open)",
            assume_yes=args.yes,
        )

        if seen:
            report.add(
                "the watcher reported the launch",
                "works",
                f"the kernel reported {len(seen)} exec(s) while the window opened",
                "This is the path the blocker daemon uses to act without polling.",
            )
    finally:
        watcher.stop()
        for child in launched:
            if child.poll() is None:
                child.kill()
            child.wait()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="Print what Anchor found installed.")
    parser.add_argument("--app", help="The identifier to check, as --list prints it.")
    parser.add_argument("--grace", type=float, default=30.0, help="Shortened grace, in seconds.")
    parser.add_argument("--demo", action="store_true", help="Use a stand-in application.")
    parser.add_argument("--yes", action="store_true", help="Do not stop to ask anything.")
    parser.add_argument("--out", default="check-results", help="Where to write the findings.")
    args = parser.parse_args(argv)

    installed = discover()

    if args.list:
        for entry in installed:
            print(f"{entry.id}\n    {entry.name} · {entry.kind} · {entry.exec_path}")
        print(f"\n{len(installed)} application(s).")
        return 0

    report = Report(machine=machine_facts())
    print(BANNER, flush=True)
    for key, value in report.machine.items():
        print(f"  {key}: {value}", flush=True)

    with tempfile.TemporaryDirectory() as workspace:
        app: InstalledApp | None
        if args.demo:
            app = make_demo_app(Path(workspace))
        elif args.app:
            found = [candidate for candidate in installed if candidate.id == args.app]
            app = found[0] if found else None
            if app is None:
                print(f"\nNo application called {args.app!r}. Try --list.", file=sys.stderr)
                return 2
        else:
            print("\nName one with --app, or see them with --list.", file=sys.stderr)
            return 2

        print(f"\nChecking:\n{describe(app)}\n", flush=True)
        if not args.yes:
            ask("This will close it. Press Enter to go on, or Ctrl-C to stop.", assume_yes=False)

        check(app, report, args)

    path = report.write(Path(args.out))
    print(f"\nFindings written to {path}", flush=True)
    print("Send me that file, or paste what you see above.", flush=True)
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
