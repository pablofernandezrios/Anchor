#!/usr/bin/env python3
"""Spike 4: is there a top-bar indicator on current GNOME? (SPEC 14.1)

GNOME Shell dropped the tray long ago and does not implement the
StatusNotifierItem specification by itself; an extension provides it. Ubuntu
ships one, other GNOME distributions do not, and SPEC 14.1 says onboarding has
to detect its absence and explain what to install. This spike works out which
situation the machine is in, and whether an indicator can actually be created.
"""

from __future__ import annotations

import os

from lib import SpikeReport, Verdict, have, main, run

WATCHER = "org.kde.StatusNotifierWatcher"

#: The extensions that provide a tray on GNOME Shell.
KNOWN_EXTENSIONS = (
    "ubuntu-appindicators@ubuntu.com",
    "appindicatorsupport@rgcjonas.gmail.com",
)


def spike(report: SpikeReport) -> None:
    session = os.environ.get("XDG_SESSION_TYPE", "unknown")
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "unknown")
    report.add(
        "session",
        Verdict.WORKS,
        f"XDG_CURRENT_DESKTOP={desktop}, XDG_SESSION_TYPE={session}",
        run("sh", "-c", "gnome-shell --version 2>/dev/null || echo 'gnome-shell absent'").text,
    )

    if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        report.add(
            "session bus",
            Verdict.UNAVAILABLE,
            "no session bus; run this spike inside the graphical session, not over plain ssh",
        )
        return

    # Is anything implementing the watcher the specification relies on?
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

    _try_creating_an_indicator(report)


def _try_creating_an_indicator(report: SpikeReport) -> None:
    """Create a real indicator through the bindings Anchor would use."""
    script = """
import sys
import gi

for namespace, version in (("AyatanaAppIndicator3", "0.1"), ("AppIndicator3", "0.1")):
    try:
        gi.require_version(namespace, version)
        module = __import__("gi.repository." + namespace, fromlist=[namespace])
        break
    except (ValueError, ImportError):
        continue
else:
    print("NO_BINDINGS")
    sys.exit(0)

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

indicator = module.Indicator.new(
    "anchor-spike", "alarm-symbolic", module.IndicatorCategory.APPLICATION_STATUS
)
indicator.set_status(module.IndicatorStatus.ACTIVE)
indicator.set_label("2:14", "0:00")

menu = Gtk.Menu()
item = Gtk.MenuItem(label="Open Anchor")
menu.append(item)
menu.show_all()
indicator.set_menu(menu)

print("CREATED", namespace)
GLib.timeout_add_seconds(3, Gtk.main_quit)
Gtk.main()
print("OK")
"""
    result = run("python3", "-c", script, timeout=30)

    if "NO_BINDINGS" in result.out:
        report.add(
            "indicator bindings",
            Verdict.FAILS,
            "neither AyatanaAppIndicator3 nor AppIndicator3 is available. Install "
            "gir1.2-ayatanaappindicator3-0.1",
            result.text[:600],
        )
        return

    if "OK" in result.out:
        report.add(
            "indicator can be created",
            Verdict.WORKS,
            "an indicator with a label was created and ran for three seconds "
            f"({result.out.strip().splitlines()[0]})",
            result.text[:600],
        )
        report.add(
            "check by eye",
            Verdict.WORKS,
            "look at the top bar while this runs: the label should read 2:14 next "
            "to an icon. SPEC 14.1 wants the icon and the remaining time",
        )
    else:
        report.add(
            "indicator can be created",
            Verdict.FAILS,
            "creating the indicator failed",
            result.text[:1200],
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
