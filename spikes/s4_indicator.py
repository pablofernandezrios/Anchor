#!/usr/bin/env python3
"""Spike 4: is there a top-bar indicator on current GNOME? (SPEC 14.1)

GNOME Shell dropped the tray long ago and does not implement the
StatusNotifierItem specification itself; an extension provides it. Ubuntu ships
one, other GNOME distributions do not, and SPEC 14.1 says onboarding has to
detect its absence and explain what to install.

There are two ways for Anchor to put an item on that bus, and the difference
matters more than it looks:

* **The AppIndicator library.** ``AyatanaAppIndicator3`` is a **GTK3** library.
  Anchor's interface is GTK4 (SPEC 14), and a process cannot load GTK3 and
  GTK4 at once, so this route forces the indicator out of ``anchor-agent`` and
  into a separate process, against SPEC 5.1.
* **The D-Bus interface directly.** The extension watches
  ``org.kde.StatusNotifierWatcher``; anything that registers there gets shown.
  Speaking it with Gio needs no GTK at all, so the indicator can stay inside
  the GTK4 agent.

The spike tries both and reports which is available.
"""

from __future__ import annotations

import os
from pathlib import Path

from lib import SpikeReport, Verdict, have, main, run

WATCHER = "org.kde.StatusNotifierWatcher"
HERE = Path(__file__).resolve().parent

#: The extensions that provide a tray on GNOME Shell.
KNOWN_EXTENSIONS = (
    "ubuntu-appindicators@ubuntu.com",
    "appindicatorsupport@rgcjonas.gmail.com",
)


def spike(report: SpikeReport) -> None:
    session = os.environ.get("XDG_SESSION_TYPE", "unknown")
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "unknown")
    graphical = session in ("wayland", "x11")
    report.add(
        "session",
        Verdict.WORKS if graphical else Verdict.UNAVAILABLE,
        f"XDG_CURRENT_DESKTOP={desktop}, XDG_SESSION_TYPE={session}"
        + (
            ""
            if graphical
            else ". This is not a graphical session, so nothing below can be "
            "confirmed by eye. Run it from a terminal inside the GNOME session"
        ),
        run("sh", "-c", "gnome-shell --version 2>/dev/null || echo 'gnome-shell absent'").text,
    )

    if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        report.add(
            "session bus",
            Verdict.UNAVAILABLE,
            "no session bus; run this spike inside the graphical session, not over plain ssh",
        )
        return

    names = run(
        "dbus-send",
        "--session",
        "--dest=org.freedesktop.DBus",
        "--type=method_call",
        "--print-reply",
        "/org/freedesktop/DBus",
        "org.freedesktop.DBus.ListNames",
    )
    watcher_present = WATCHER in names.out
    report.add(
        "StatusNotifierWatcher on the bus",
        Verdict.WORKS if watcher_present else Verdict.FAILS,
        f"{WATCHER} is registered, so an indicator will be shown"
        if watcher_present
        else f"{WATCHER} is absent: nothing will display an indicator until the "
        "AppIndicator extension is installed and enabled",
    )

    if have("gnome-extensions"):
        listing = run("gnome-extensions", "list", "--enabled")
        enabled = [name for name in KNOWN_EXTENSIONS if name in listing.out]
        report.add(
            "AppIndicator extension",
            Verdict.WORKS if enabled else Verdict.FAILS,
            f"enabled: {', '.join(enabled)}"
            if enabled
            else "no known AppIndicator extension is enabled. On Ubuntu install "
            "gnome-shell-extension-appindicator; elsewhere the extension from "
            "extensions.gnome.org is needed",
            listing.text[:800],
        )

    _check_gtk3_route(report)
    _check_dbus_route(report, graphical=graphical)


def _check_gtk3_route(report: SpikeReport) -> None:
    """Route one: the AppIndicator library, which drags GTK3 along."""
    probe = """
import gi
for namespace in ("AyatanaAppIndicator3", "AppIndicator3"):
    try:
        gi.require_version(namespace, "0.1")
        __import__("gi.repository." + namespace, fromlist=[namespace])
        print("FOUND", namespace)
        break
    except (ValueError, ImportError):
        continue
else:
    print("ABSENT")
"""
    result = run("python3", "-c", probe, timeout=30)

    if "FOUND" in result.out:
        namespace = result.out.split()[1]
        report.add(
            "AppIndicator library (GTK3)",
            Verdict.RULED_OUT,
            f"{namespace} is installed, but it is a GTK3 library and Anchor's "
            "interface is GTK4. One process cannot load both, so using it would "
            "push the indicator out of anchor-agent into a process of its own, "
            "against SPEC 5.1. Preferred only if the D-Bus route below fails",
            result.text[:400],
        )
        return

    report.add(
        "AppIndicator library (GTK3)",
        Verdict.UNAVAILABLE,
        "neither AyatanaAppIndicator3 nor AppIndicator3 is installed "
        "(gir1.2-ayatanaappindicator3-0.1). This is not a problem: the library "
        "is GTK3 and Anchor's interface is GTK4, so this route was the fallback "
        "rather than the plan",
        result.text[:400],
    )


def _check_dbus_route(report: SpikeReport, *, graphical: bool) -> None:
    """Route two: speak StatusNotifierItem directly, with no GTK at all."""
    probe = HERE / "_sni_probe.py"
    if not probe.exists():
        report.add(
            "D-Bus StatusNotifierItem",
            Verdict.UNAVAILABLE,
            f"{probe.name} is missing next to this spike",
        )
        return

    result = run("python3", str(probe), timeout=60)
    out = result.out

    if "NO_BUS" in out:
        report.add(
            "D-Bus StatusNotifierItem",
            Verdict.UNAVAILABLE,
            "could not reach the session bus",
            result.text[:600],
        )
        return

    if "ModuleNotFoundError" in result.text or "No module named 'gi'" in result.text:
        report.add(
            "D-Bus StatusNotifierItem",
            Verdict.UNAVAILABLE,
            "PyGObject is not installed (python3-gi), so the route could not be tested",
            result.text[:600],
        )
        return

    registered = "REGISTERED" in out
    listed = False
    for line in out.splitlines():
        if line.startswith("WATCHER_LISTS"):
            parts = line.split()
            listed = len(parts) >= 4 and parts[3] != "0"

    if registered and listed:
        report.add(
            "D-Bus StatusNotifierItem",
            Verdict.WORKS,
            "an indicator was registered with the watcher and the watcher listed "
            "it back, using Gio alone. No GTK3 library is needed, so the "
            "indicator can stay inside the GTK4 agent as SPEC 5.1 wants",
            result.text[:900],
        )
    elif registered:
        report.add(
            "D-Bus StatusNotifierItem",
            Verdict.FAILS,
            "the watcher accepted the registration but did not list the item "
            "back, so something is rejecting it quietly",
            result.text[:900],
        )
    else:
        report.add(
            "D-Bus StatusNotifierItem",
            Verdict.FAILS,
            "registering a StatusNotifierItem over D-Bus failed",
            result.text[:900],
        )

    if registered and graphical:
        report.add(
            "check by eye",
            Verdict.WORKS,
            "while that ran, the top bar should have shown an alarm icon with "
            "the label 2:14 beside it for six seconds. SPEC 14.1 wants both the "
            "icon and the remaining time, so note whether the label appeared or "
            "only the icon",
        )


if __name__ == "__main__":
    raise SystemExit(
        main(
            spike,
            SpikeReport(
                spike="s4-indicator",
                question="Can Anchor show a top-bar indicator on this GNOME?",
            ),
        )
    )
