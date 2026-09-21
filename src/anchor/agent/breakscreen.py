"""What the break overlay says (SPEC 10, ADR 2).

The approved mockup draws this screen: a word, a countdown, a line telling the
user what to do with the next few minutes, what comes after, and — when the
profile allows it — a way out with the cost written underneath.

    DESCANSO
    04:12
    Levántate, estira y aparta la vista de la pantalla.
    Después: 50 min de trabajo · Sesión Estudio
    [ Posponer 5 minutos ]
    1 aplazamiento disponible · Dureza moderada

Everything here is words and numbers, so it is decided in the same place as
the rest of the agent's decisions and tested without a screen. The GTK windows
that carry it are next door.

The countdown is minutes and seconds, unlike the indicator's. A panel label
that moved every second would be noise; a break countdown that did not would
look broken, because watching it is most of what the screen is for.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from anchor.cli.durations import format_duration
from anchor.protocol.types import BreakHardness, SessionPhase

#: What to do with the break, as the mockup words it.
ADVICE = "Get up, stretch, and look away from the screen."
LONG_ADVICE = "Get up and leave the screen for a while."


def format_break_countdown(seconds: float) -> str:
    """``04:12``. Minutes and seconds, zero-padded, as the mockup draws it."""
    total = int(max(0.0, seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


@dataclass(frozen=True, slots=True)
class BreakScreen:
    """One break overlay, as words."""

    title: str = "BREAK"
    countdown: str = "00:00"
    advice: str = ADVICE
    after: str = ""
    postpone: str = ""
    """The postpone button's label, or empty when it is not offered."""

    skip: str = ""
    """The skip button's label, or empty when it is not offered."""

    footnote: str = ""
    """What the buttons cost, and what this profile's hardness means."""


def screen_for(status: dict[str, Any], *, postpone_seconds: int = 300) -> BreakScreen | None:
    """The overlay for this status, or ``None`` when none should be shown.

    ``None`` is the common case: most of a session is not a break.
    """
    if not status.get("active"):
        return None

    rest = status.get("break")
    if not isinstance(rest, dict):
        return None
    if str(rest.get("phase", "")) != str(SessionPhase.BREAK):
        return None

    long = bool(rest.get("long"))
    hardness = str(rest.get("hardness", BreakHardness.MODERATE))
    can_postpone = bool(rest.get("can_postpone"))
    can_skip = bool(rest.get("can_skip"))

    return BreakScreen(
        title="LONG BREAK" if long else "BREAK",
        countdown=format_break_countdown(float(rest.get("remaining_seconds", 0))),
        advice=LONG_ADVICE if long else ADVICE,
        after=_after(status),
        postpone=(f"Postpone {format_duration(postpone_seconds)}" if can_postpone else ""),
        skip="Skip break" if can_skip else "",
        footnote=_footnote(hardness, can_postpone=can_postpone, can_skip=can_skip),
    )


def _after(status: dict[str, Any]) -> str:
    """``Next: 50 min of work · Study session``, as the mockup words it."""
    profile = str(status.get("profile", "")).strip()
    session = f" · {profile} session" if profile else ""
    return f"Then: back to work{session}"


def _footnote(hardness: str, *, can_postpone: bool, can_skip: bool) -> str:
    """The cost of the buttons, and what this hardness means (SPEC 10).

    A Mandatory break says what it is rather than showing nothing: a screen
    with no way out and no explanation looks like a program that has hung.
    """
    named = f"{hardness.capitalize()} breaks"
    if hardness == str(BreakHardness.MANDATORY):
        return f"{named} cannot be skipped or postponed."
    if hardness == str(BreakHardness.MODERATE):
        if can_postpone:
            return f"{named} can be postponed once, and not skipped."
        return f"{named} allow one postponement, and it is used."
    if can_skip or can_postpone:
        return f"{named} can be skipped or postponed freely."
    return named
