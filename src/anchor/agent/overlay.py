"""The fullscreen break overlay (SPEC 10, ADR 2).

One window per monitor, carrying the screen :mod:`breakscreen` composed. What
it says was decided there and tested without a display; what is here is GTK,
and GTK is checked by looking at it (``tools/check_overlay.py``).

ADR 2 settled what this can and cannot do. On Wayland an ordinary client
cannot stay above everything else, cannot grab the keyboard, and cannot stop
the user switching away — and none of that is a defect to engineer around. So
the overlay does not trap anyone. It covers every monitor, it says what is
happening, and if it loses focus it asks the compositor to present it again.
Whether the compositor agrees is the compositor's decision.

A break therefore ends when its time is up, not when the user dismisses it.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from anchor.agent.breakscreen import BreakScreen

log = logging.getLogger("anchor-agent")

#: How long to leave the window alone after asking for it to be presented.
#: Without this, a compositor that refuses gets asked again on every focus
#: change it causes, which is a loop with a user inside it.
REPRESENT_SECONDS = 3.0

STYLE = b"""
window.anchor-break {
  background-color: #1c1b22;
}
.anchor-break-title {
  color: #c9c6d4;
  letter-spacing: 6px;
}
.anchor-break-countdown {
  color: #ffffff;
}
.anchor-break-advice {
  color: #c9c6d4;
}
.anchor-break-footnote {
  color: #8d8a97;
}
"""


class OverlayUnavailableError(RuntimeError):
    """GTK is missing, or there is no display to put a window on."""


def _gtk() -> Any:
    """Import GTK 4, or explain what is missing.

    Imported here rather than at the top of the module so the agent runs on a
    machine with no display at all, which is where its tests run.
    """
    try:
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Gdk", "4.0")
        from gi.repository import Gdk, GLib, Gtk
    except (ImportError, ValueError) as error:
        raise OverlayUnavailableError(
            f"GTK 4 is not available ({error}). Install gir1.2-gtk-4.0."
        ) from error
    return Gtk, Gdk, GLib


class BreakOverlay:
    """Covers every monitor for the length of a break."""

    def __init__(
        self,
        *,
        on_postpone: Callable[[], None] | None = None,
        on_skip: Callable[[], None] | None = None,
    ) -> None:
        self._gtk, self._gdk, self._glib = _gtk()
        if not self._gtk.init_check():
            raise OverlayUnavailableError(
                "GTK could not open a display. The agent is a user service and "
                "needs the user's own graphical session."
            )
        self._on_postpone = on_postpone
        self._on_skip = on_skip
        self._windows: list[Any] = []
        self._labels: dict[str, list[Any]] = {}
        self._buttons: dict[str, list[Any]] = {}
        self._last_present = 0.0
        self._showing = False

    @property
    def showing(self) -> bool:
        return self._showing

    @property
    def monitors(self) -> int:
        return len(self._windows)

    # -- what the agent calls --------------------------------------------

    def show(self, screen: BreakScreen) -> None:
        """Put the overlay up, or update the one already there."""
        if not self._windows:
            self._build(screen)
        self._update(screen)
        if not self._showing:
            for window in self._windows:
                window.present()
            self._showing = True

    def hide(self) -> None:
        """Take it down. The break is over, one way or another."""
        for window in self._windows:
            window.destroy()
        self._windows.clear()
        self._labels.clear()
        self._buttons.clear()
        self._showing = False

    # -- the windows -----------------------------------------------------

    def _build(self, screen: BreakScreen) -> None:
        """One fullscreen window per monitor (SPEC 10)."""
        display = self._gdk.Display.get_default()
        if display is None:  # pragma: no cover - checked by init_check above
            raise OverlayUnavailableError("there is no display")

        monitors = display.get_monitors()
        count = monitors.get_n_items()
        for index in range(count):
            self._windows.append(self._window(monitors.get_item(index), screen))

        # A monitor plugged in during a break should be covered too, rather
        # than becoming the one screen with the distraction on it.
        monitors.connect("items-changed", self._monitors_changed)
        log.info("break overlay on %d monitor(s)", count)

    def _monitors_changed(self, monitors: Any, *_args: object) -> None:
        if not self._showing:
            return
        wanted = monitors.get_n_items()
        if wanted <= len(self._windows):
            return
        screen = self._current_screen()
        for index in range(len(self._windows), wanted):
            window = self._window(monitors.get_item(index), screen)
            self._windows.append(window)
            window.present()
        log.info("break overlay now on %d monitor(s)", wanted)

    def _window(self, monitor: Any, screen: BreakScreen) -> Any:
        gtk = self._gtk
        window = gtk.Window()
        window.set_decorated(False)
        window.set_deletable(False)
        window.add_css_class("anchor-break")
        self._apply_style(window)

        box = gtk.Box(orientation=gtk.Orientation.VERTICAL, spacing=18)
        box.set_valign(gtk.Align.CENTER)
        box.set_halign(gtk.Align.CENTER)

        box.append(self._label("title", screen.title, 'font_desc="Sans Bold 18"'))
        box.append(self._label("countdown", screen.countdown, 'font_desc="Sans Light 96"'))
        box.append(self._label("advice", screen.advice, 'font_desc="Sans 16"'))
        box.append(self._label("after", screen.after, 'font_desc="Sans 13"'))

        buttons = gtk.Box(orientation=gtk.Orientation.HORIZONTAL, spacing=12)
        buttons.set_halign(gtk.Align.CENTER)
        buttons.append(self._button("postpone", screen.postpone, self._postponed))
        buttons.append(self._button("skip", screen.skip, self._skipped))
        box.append(buttons)

        box.append(self._label("footnote", screen.footnote, 'font_desc="Sans 11"'))
        window.set_child(box)

        window.fullscreen_on_monitor(monitor)
        # ADR 2: it cannot hold the screen, so it asks to come back instead.
        window.connect("notify::is-active", self._focus_changed)
        return window

    def _label(self, name: str, text: str, font: str) -> Any:
        label = self._gtk.Label()
        label.set_markup(f"<span {font}>{_escape(text)}</span>")
        label.add_css_class(f"anchor-break-{name}")
        label.set_wrap(True)
        label.set_justify(self._gtk.Justification.CENTER)
        self._labels.setdefault(name, []).append(label)
        return label

    def _button(self, name: str, text: str, clicked: Callable[[Any], None]) -> Any:
        button = self._gtk.Button(label=text)
        button.connect("clicked", clicked)
        button.set_visible(bool(text))
        self._buttons.setdefault(name, []).append(button)
        return button

    def _apply_style(self, window: Any) -> None:
        """Dark, plain and unmistakable. Styling is not worth failing over."""
        try:
            provider = self._gtk.CssProvider()
            if hasattr(provider, "load_from_string"):
                provider.load_from_string(STYLE.decode("utf-8"))
            else:  # pragma: no cover - older GTK
                provider.load_from_data(STYLE)
            self._gtk.StyleContext.add_provider_for_display(window.get_display(), provider, 800)
        except Exception as error:
            log.info("the overlay could not be styled: %s", error)

    # -- keeping it there ------------------------------------------------

    def _focus_changed(self, window: Any, *_args: object) -> None:
        """Ask to be presented again, at most every few seconds (ADR 2)."""
        if not self._showing or window.is_active():
            return
        now = time.monotonic()
        if now - self._last_present < REPRESENT_SECONDS:
            return
        self._last_present = now
        window.present()

    # -- the buttons -----------------------------------------------------

    def _postponed(self, _button: Any) -> None:
        if self._on_postpone is not None:
            self._on_postpone()

    def _skipped(self, _button: Any) -> None:
        if self._on_skip is not None:
            self._on_skip()

    # -- updating --------------------------------------------------------

    def _update(self, screen: BreakScreen) -> None:
        fonts = {
            "title": 'font_desc="Sans Bold 18"',
            "countdown": 'font_desc="Sans Light 96"',
            "advice": 'font_desc="Sans 16"',
            "after": 'font_desc="Sans 13"',
            "footnote": 'font_desc="Sans 11"',
        }
        for name, text in (
            ("title", screen.title),
            ("countdown", screen.countdown),
            ("advice", screen.advice),
            ("after", screen.after),
            ("footnote", screen.footnote),
        ):
            for label in self._labels.get(name, ()):
                label.set_markup(f"<span {fonts[name]}>{_escape(text)}</span>")

        for name, text in (("postpone", screen.postpone), ("skip", screen.skip)):
            for button in self._buttons.get(name, ()):
                button.set_label(text)
                # A postponement that has been used stops being offered
                # mid-break, rather than failing when it is clicked.
                button.set_visible(bool(text))

        self._screen = screen

    def _current_screen(self) -> BreakScreen:
        return getattr(self, "_screen", BreakScreen())


def _escape(text: str) -> str:
    """Pango markup is XML, and a profile name is whatever the user typed."""
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )
