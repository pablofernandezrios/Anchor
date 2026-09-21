"""What the top bar shows (SPEC 14.1).

The indicator is the only part of Anchor a user sees all day, so what it says
matters more than how it is drawn. This module decides the words; the D-Bus
adapter beside it does the drawing, and can be replaced without touching any
of the decisions here.

Three of those decisions are worth stating:

* **Minutes, rounded up.** The approved mockup shows ``2:14`` in the top bar,
  not ``2:14:37``. Seconds in a panel are noise, and a label that redraws every
  second is worse than one that redraws every minute. Rounding up rather than
  down keeps the last minute from reading ``0:00`` for a full sixty seconds,
  which looks like something has stopped working.
* **Gone when nothing is running.** SPEC 14.1 asks for it while a session runs,
  and an icon that sits there when nothing is blocked teaches the user to
  ignore it.
* **Honest when the engine is unreachable.** An indicator that kept counting
  down from memory after the engine died would be inventing the one number
  people trust it for. It says so instead.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from anchor.protocol.types import Level, SessionPhase

#: The icon shown while a session runs. Anchor ships its own with the packages
#: (Milestone 9); until then this is a name every icon theme carries.
ICON_ACTIVE = "alarm-symbolic"

#: Shown when the engine cannot be reached, so the label is not to be trusted.
ICON_UNKNOWN = "dialog-question-symbolic"

#: What an unmeasurable remaining time reads as.
UNKNOWN_LABEL = "—"

#: Tells the panel how wide the label will get, so it stops jumping about.
LABEL_GUIDE = "0:00"


def format_label(seconds: float) -> str:
    """The countdown as the top bar shows it: hours and minutes, rounded up."""
    minutes = math.ceil(max(0.0, seconds) / 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}"


def format_clock(timestamp: float) -> str:
    """A wall-clock time as ``14:30``."""
    try:
        return datetime.fromtimestamp(timestamp).strftime("%H:%M")
    except (OverflowError, OSError, ValueError):
        return "?"


@dataclass(frozen=True, slots=True)
class MenuItem:
    """One line of the indicator's menu (SPEC 14.1)."""

    label: str
    action: str = ""
    """What clicking it does. Empty means the line is information only."""

    enabled: bool = True


@dataclass(frozen=True, slots=True)
class IndicatorView:
    """Everything the panel needs, worked out in one place."""

    visible: bool = False
    icon: str = ICON_ACTIVE
    label: str = ""
    guide: str = LABEL_GUIDE
    title: str = "Anchor"
    tooltip: str = ""
    menu: tuple[MenuItem, ...] = ()


@dataclass
class IndicatorModel:
    """The latest the agent knows, and what the panel should make of it."""

    status: dict[str, Any] = field(default_factory=dict)
    connected: bool = True

    def update_status(self, status: dict[str, Any]) -> None:
        self.status = dict(status)

    def note_connection(self, connected: bool) -> None:
        self.connected = connected

    def note_event(self, event: str, payload: dict[str, Any]) -> None:
        """Take in an event that changes what the indicator should say.

        Most events carry the whole status, because the engine sends it with
        every tick anyway and one shape is easier to be right about than six.
        The ones that do not are the ones that end a session.
        """
        if event == "session.ended":
            self.status = {"active": False}
        elif "active" in payload:
            self.status = dict(payload)

    # -- what the panel draws --------------------------------------------

    @property
    def view(self) -> IndicatorView:
        if not self.connected:
            return self._unreachable()
        if not self.status.get("active"):
            return IndicatorView(visible=False)
        return self._session()

    def _unreachable(self) -> IndicatorView:
        """The engine is not answering.

        Kept visible while a session was running, because disappearing would
        say "your session ended", which is the one thing that must never be
        said wrongly. With nothing running there is nothing to be wrong about.
        """
        if not self.status.get("active"):
            return IndicatorView(visible=False)
        return IndicatorView(
            visible=True,
            icon=ICON_UNKNOWN,
            label=UNKNOWN_LABEL,
            tooltip="Anchor cannot reach its engine, so the time left is unknown.",
            menu=(
                MenuItem("Anchor cannot reach its engine", enabled=False),
                MenuItem("Blocking stays in place until it answers", enabled=False),
                MenuItem("Open Anchor", action="open"),
            ),
        )

    def _session(self) -> IndicatorView:
        status = self.status
        remaining = float(status.get("remaining_seconds", 0))
        level = str(status.get("level", Level.SOFT))
        profile = str(status.get("profile", "?"))
        attempts = int(status.get("blocked_attempts", 0))
        apps_closed = int(status.get("app_blocks", 0))
        phase = str(status.get("phase", SessionPhase.WORKING))
        ends_at = float(status.get("ends_at", 0))

        label = format_label(remaining)
        return IndicatorView(
            visible=True,
            icon=ICON_ACTIVE,
            label=label,
            tooltip=f"{profile} · {level.capitalize()} · {label} left",
            menu=self._menu(
                profile=profile,
                level=level,
                label=label,
                ends_at=ends_at,
                attempts=attempts,
                apps_closed=apps_closed,
                phase=phase,
            ),
        )

    def _menu(
        self,
        *,
        profile: str,
        level: str,
        label: str,
        ends_at: float,
        attempts: int,
        apps_closed: int,
        phase: str,
    ) -> tuple[MenuItem, ...]:
        """The lines SPEC 14.1 lists, in the order it lists them."""
        items = [
            MenuItem(f"{profile} · {level.capitalize()}", enabled=False),
            MenuItem(f"{label} left · ends at {format_clock(ends_at)}", enabled=False),
        ]

        if phase == str(SessionPhase.BREAK):
            items.append(MenuItem("On a break", enabled=False))

        items.append(MenuItem(f"Blocked attempts: {attempts}", enabled=False))
        if apps_closed:
            items.append(MenuItem(f"Applications closed: {apps_closed}", enabled=False))

        items.append(MenuItem("Extend session", action="extend"))
        items.append(MenuItem("Open Anchor", action="open"))
        return tuple(items)
