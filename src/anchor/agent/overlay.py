"""The fullscreen break overlay (SPEC 10, ADR 2).

One window per monitor, carrying the screen :mod:`breakscreen` composed. What
it says was decided there and tested without a display; what is here is GTK,
and GTK is checked by looking at it (``tools/check_overlay.py``).

ADR 2 settled what this can and cannot do. On Wayland an ordinary client
cannot stay above everything else, cannot grab the keyboard, and cannot stop
the user switching away — and none of that is a defect to engineer around. So
the overlay does not trap anyone. It covers every monitor, it says what is
happening, and while it is not the focused window it keeps asking the
compositor to bring it back. Whether the compositor agrees is the compositor's
decision, and on GNOME the answer is often to mark the window as wanting
attention rather than to raise it.

Asking *steadily* rather than once is the part that makes ADR 2's promise
real. A single request when focus is lost is a request that can be ignored,
after which Anchor would go quiet for the rest of the break.

A break therefore ends when its time is up, not when the user dismisses it.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from anchor.agent.breakscreen import BreakScreen

log = logging.getLogger("anchor-agent")

#: How often to ask, at most, while the overlay is not the focused window.
#: Without a limit a compositor that refuses gets asked again on every focus
#: change it causes, which is a loop with a user inside it.
REPRESENT_SECONDS = 3.0


def should_reassert(*, showing: bool, active: bool, since_last: float) -> bool:
    """Whether to ask the compositor to bring the overlay back (ADR 2).

    Kept here, away from GTK, because it is the only decision in this module
    and the first run on a real desktop showed it mattered: asking once when
    focus is lost is not "the overlay reasserts itself", it is a single
    request a compositor is free to ignore, after which Anchor goes quiet for
    the rest of the break. Asking again, steadily, is the whole of what ADR 2
    promised — and the whole of what Wayland allows.
    """
    if not showing or active:
        return False
    return since_last >= REPRESENT_SECONDS


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
        self._watch: int | None = None
        self.attempts = 0
        """How many times the compositor has been asked to bring it back."""

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
            # Steadily, not once. A single request when focus is lost is a
            # request the compositor can ignore, after which Anchor says
            # nothing for the rest of the break (ADR 2).
            self._watch = self._glib.timeout_add(int(REPRESENT_SECONDS * 1000), self._reassert)

    def hide(self) -> None:
        """Take it down. The break is over, one way or another."""
        if self._watch is not None:
            self._glib.source_remove(self._watch)
            self._watch = None
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
        """Losing focus is answered at once, and then by the timer."""
        self._ask_again(active=window.is_active())

    def _reassert(self) -> bool:
        """Keep asking for as long as the break lasts (ADR 2)."""
        if not self._showing:
            return False
        self._ask_again(active=any(window.is_active() for window in self._windows))
        return True

    def _ask_again(self, *, active: bool) -> None:
        if not should_reassert(
            showing=self._showing,
            active=active,
            since_last=time.monotonic() - self._last_present,
        ):
            return

        self._last_present = time.monotonic()
        self.attempts += 1
        for window in self._windows:
            window.present()
        log.debug("asked the compositor to bring the overlay back (%d)", self.attempts)

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
