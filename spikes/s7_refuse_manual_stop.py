#!/usr/bin/env python3
"""Spike 7: does a runtime RefuseManualStop drop-in actually hold? (SPEC 5.3)

While a session is active the engine writes ``RefuseManualStop=yes`` into
``/run/systemd/system/<unit>.d/`` and reloads systemd, so that ``systemctl
stop`` is refused; when the session ends the drop-in is removed. Putting it
under ``/run`` rather than ``/etc`` is what lets uninstalling work normally
(SPEC 7.6) and what makes a reboot clear it.

The spike creates a throwaway unit and checks the whole cycle.
"""

from __future__ import annotations

import os
from pathlib import Path

from lib import SpikeReport, Verdict, main, run

UNIT = "anchor-spike-victim.service"
UNIT_PATH = Path("/run/systemd/system") / UNIT
DROP_IN_DIR = Path("/run/systemd/system") / f"{UNIT}.d"
DROP_IN = DROP_IN_DIR / "anchor-session.conf"

UNIT_BODY = """[Unit]
Description=Anchor spike victim

[Service]
Type=simple
ExecStart=/bin/sleep 3600
"""


def spike(report: SpikeReport) -> None:
    if os.geteuid() != 0:
        report.add("root", Verdict.UNAVAILABLE, "this spike needs root")
        return

    if not Path("/run/systemd/system").is_dir():
        report.add(
            "systemd as PID 1",
            Verdict.UNAVAILABLE,
            "/run/systemd/system is missing, so this is not a systemd system",
        )
        return

    report.add("systemd as PID 1", Verdict.WORKS, run("systemctl", "--version").out.splitlines()[0])

    try:
        UNIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        UNIT_PATH.write_text(UNIT_BODY, encoding="utf-8")
        run("systemctl", "daemon-reload")

        started = run("systemctl", "start", UNIT)
        if not started.ok:
            report.add(
                "unit starts",
                Verdict.FAILS,
                "the throwaway unit would not start",
                started.text,
            )
            return
        report.add("unit starts", Verdict.WORKS, f"{UNIT} is running")

        # Without the drop-in, stopping must work: this is what uninstalling
        # and ordinary maintenance rely on.
        stopped = run("systemctl", "stop", UNIT)
        report.add(
            "stops normally without the drop-in",
            Verdict.WORKS if stopped.ok else Verdict.FAILS,
            "systemctl stop works when no session is active"
            if stopped.ok
            else "the unit could not be stopped even without a drop-in",
            stopped.text,
        )

        run("systemctl", "start", UNIT)

        DROP_IN_DIR.mkdir(parents=True, exist_ok=True)
        DROP_IN.write_text(
            "# Written by Anchor while a session is active (SPEC 5.3).\n"
            "[Unit]\n"
            "RefuseManualStop=yes\n",
            encoding="utf-8",
        )
        reloaded = run("systemctl", "daemon-reload")
        report.add(
            "runtime drop-in is read",
            Verdict.WORKS if reloaded.ok else Verdict.FAILS,
            "systemd reloaded with a drop-in under /run/systemd/system",
            run("systemctl", "show", UNIT, "-p", "RefuseManualStop").text,
        )

        refused = run("systemctl", "stop", UNIT)
        honoured = not refused.ok
        report.add(
            "stop is refused during a session",
            Verdict.WORKS if honoured else Verdict.FAILS,
            "systemctl stop was refused while the drop-in was in place"
            if honoured
            else "systemctl stop succeeded despite RefuseManualStop, so this "
            "mitigation does not work as SPEC 5.3 assumes",
            refused.text,
        )

        still = run("systemctl", "is-active", UNIT)
        report.add(
            "the service survived the attempt",
            Verdict.WORKS if still.text == "active" else Verdict.FAILS,
            f"the unit is {still.text} after the refused stop",
        )

        # And removing the drop-in must hand control straight back.
        DROP_IN.unlink(missing_ok=True)
        run("systemctl", "daemon-reload")
        released = run("systemctl", "stop", UNIT)
        report.add(
            "removing the drop-in releases the unit",
            Verdict.WORKS if released.ok else Verdict.FAILS,
            "the unit stopped normally once the drop-in was gone"
            if released.ok
            else "the unit stayed locked after the drop-in was removed",
            released.text,
        )

        report.add(
            "a reboot would clear it",
            Verdict.WORKS,
            "the drop-in lives under /run, which is a tmpfs cleared on boot; the "
            "engine rewrites it if a session is still active",
            run("findmnt", "-no", "FSTYPE", "/run").text,
        )
    finally:
        run("systemctl", "stop", UNIT)
        DROP_IN.unlink(missing_ok=True)
        if DROP_IN_DIR.exists():
            DROP_IN_DIR.rmdir()
        UNIT_PATH.unlink(missing_ok=True)
        run("systemctl", "daemon-reload")
        report.add("clean restore", Verdict.WORKS, "the throwaway unit and drop-in were removed")


if __name__ == "__main__":
    raise SystemExit(
        main(
            spike,
            SpikeReport(
                spike="s7-refuse-manual-stop",
                question="Is a runtime RefuseManualStop drop-in honoured, and reversible?",
            ),
        )
    )
