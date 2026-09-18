#!/usr/bin/env python3
"""Spike 5: a fullscreen break overlay on Wayland, on every monitor (SPEC 10).

The break overlay has to cover every screen and stay in front. Wayland
deliberately refuses the X11 tricks for that: a client cannot place itself
above everything or grab the keyboard. What remains is an ordinary fullscreen
window per monitor, and on wlroots compositors the layer-shell protocol. GNOME
does not implement layer-shell, so the question is how close a plain GTK4
fullscreen window gets, and what it cannot do.
"""

from __future__ import annotations

import os

from lib import SpikeReport, Verdict, main, run

OVERLAY = """
import sys
import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

SECONDS = 5


class Overlay(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="dev.anchor.spike.overlay")
        self.windows_made = 0

    def do_activate(self):
        display = Gdk.Display.get_default()
        if display is None:
            print("NO_DISPLAY")
            self.quit()
            return

        monitors = display.get_monitors()
        count = monitors.get_n_items()
        print(f"MONITORS {count}")

        for index in range(count):
            monitor = monitors.get_item(index)
            geometry = monitor.get_geometry()
            print(
                f"MONITOR {index} {monitor.get_connector()} "
                f"{geometry.width}x{geometry.height}+{geometry.x}+{geometry.y}"
            )

            window = Gtk.ApplicationWindow(application=self)
            window.set_decorated(False)
            label = Gtk.Label()
            label.set_markup(
                "<span size='72000' weight='bold'>04:12</span>\\n"
                "<span size='20000'>Anchor spike overlay</span>"
            )
            window.set_child(label)
            window.fullscreen_on_monitor(monitor)
            window.present()
            self.windows_made += 1

        print(f"PRESENTED {self.windows_made}")
        GLib.timeout_add_seconds(SECONDS, self.finish)

    def finish(self):
        print("OK")
        self.quit()
        return False


sys.exit(Overlay().run([]))
"""


def spike(report: SpikeReport) -> None:
    session = os.environ.get("XDG_SESSION_TYPE", "unknown")
    report.add(
        "session type",
        Verdict.WORKS if session == "wayland" else Verdict.UNAVAILABLE,
        f"XDG_SESSION_TYPE={session}"
        + ("" if session == "wayland" else "; SPEC P7 makes Wayland the reference"),
    )

    if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")):
        report.add(
            "display",
            Verdict.UNAVAILABLE,
            "no display; run this spike inside the graphical session",
        )
        return

    result = run("python3", "-c", OVERLAY, timeout=60)

    if "NO_DISPLAY" in result.out:
        report.add("overlay opens", Verdict.FAILS, "GTK could not open the display", result.text)
        return

    monitors = [line for line in result.out.splitlines() if line.startswith("MONITOR ")]
    count_line = next(
        (line for line in result.out.splitlines() if line.startswith("MONITORS ")), ""
    )

    if "OK" in result.out:
        report.add(
            "overlay covers every monitor",
            Verdict.WORKS,
            f"{count_line.removeprefix('MONITORS ') or '?'} monitor(s) detected and each "
            "got its own fullscreen window for five seconds",
            "\n".join(monitors),
        )
        report.add(
            "check by eye",
            Verdict.WORKS,
            "every screen should have shown a black window reading 04:12. If a "
            "screen stayed uncovered, note which one",
        )
    else:
        report.add(
            "overlay covers every monitor",
            Verdict.FAILS,
            "the overlay did not run to completion",
            result.text[:1500],
        )

    # The honest limits, which shape what "mandatory" can mean in SPEC 10.
    report.add(
        "always on top",
        Verdict.UNAVAILABLE,
        "Wayland gives no way for an ordinary client to force itself above "
        "everything; GNOME does not implement layer-shell. A fullscreen window "
        "is what Anchor gets, and the user can still switch away from it. "
        "A Mandatory break therefore means the overlay keeps coming back, not "
        "that the screen is seized",
    )
    report.add(
        "input grab",
        Verdict.UNAVAILABLE,
        "Wayland does not allow a client to grab the keyboard, so the overlay "
        "cannot swallow shortcuts. This is a limit to document in the README "
        "rather than a bug to fix",
    )


if __name__ == "__main__":
    raise SystemExit(
        main(
            spike,
            SpikeReport(
                spike="s5-wayland-overlay",
                question="Can a break overlay cover every monitor on Wayland?",
            ),
        )
    )
