"""What the Home screen says (SPEC 14, approved mockup, page 1).

The mockup draws one screen: an active session with its countdown, its next
break, what it has refused, what leaving would cost, and two buttons; then the
schedules that are coming; then three figures for today.

Everything here is words and numbers worked out from one status, so it is
decided in this module and drawn next door. That split has already caught two
real bugs in the agent, and the interface is where most of Anchor's sentences
live.

Three decisions worth stating.

**The countdown keeps its seconds.** The top bar shows ``2:14`` because a
label that redraws every second is noise in a panel. This screen is being
looked at on purpose, and the mockup draws ``2:14:37``.

**Home says what leaving costs before anybody wants to leave.** The mockup
gives it a heading of its own, and the honest moment to read "this cannot be
cancelled" is now, not when it is too late for it to matter.

**A lost engine does not blank the screen.** An interface that cleared its
session card when the socket went quiet would be saying "your session ended",
which is the one thing Anchor must never say wrongly. The card stays, with a
banner above it saying the numbers may be stale.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from anchor.cli.durations import format_countdown, format_duration
from anchor.gui.i18n import N_, _
from anchor.protocol.types import BreakType, Level, SessionOrigin, SessionPhase, Valve

#: Schedules shown on Home. The mockup draws two; more belongs on the
#: Schedules screen, which is one click away and draws the whole week.
UPCOMING = 3

#: Refused domains named under the attempt count, as the mockup does.
NAMED_TARGETS = 2


@dataclass(frozen=True, slots=True)
class Action:
    """A button, and the request it sends."""

    label: str
    request: str
    enabled: bool = True
    style: str = "normal"
    """``suggested`` for the obvious one, ``destructive`` for leaving."""

    hint: str = ""
    """Why it is disabled, when it is."""


@dataclass(frozen=True, slots=True)
class Figure:
    """One of today's numbers, and what it counts."""

    value: str
    label: str


@dataclass(frozen=True, slots=True)
class ScheduleLine:
    """One upcoming schedule, as the mockup lists it."""

    when: str
    name: str
    level: str
    now: bool = False


@dataclass(frozen=True, slots=True)
class SessionCard:
    """The active session, as the mockup draws it."""

    profile: str
    level: str
    started: str
    countdown: str
    remaining: str

    break_title: str
    break_when: str
    break_detail: str

    attempts: str
    attempts_detail: str

    exit_how: str
    exit_rule: str
    exit_pending: str = ""

    actions: tuple[Action, ...] = ()


@dataclass(frozen=True, slots=True)
class IdleCard:
    """What Home offers when nothing is running."""

    title: str
    detail: str
    action: Action


@dataclass(frozen=True, slots=True)
class HomeView:
    """The whole screen."""

    session: SessionCard | None = None
    idle: IdleCard | None = None
    schedules: tuple[ScheduleLine, ...] = ()
    skips: str = ""
    today: tuple[Figure, ...] = field(default_factory=tuple)
    banner: str = ""


def home_view(
    status: dict[str, Any],
    *,
    schedules: dict[str, Any] | None = None,
    stats: dict[str, Any] | None = None,
    connected: bool = True,
    apps: dict[str, str] | None = None,
    today: int | None = None,
) -> HomeView:
    """Everything the Home screen shows, from one moment's answers."""
    listing = schedules or {}
    figures = stats or {}

    return HomeView(
        session=_card(status, figures, apps or {}) if status.get("active") else None,
        idle=None if status.get("active") else _idle(),
        schedules=_upcoming(listing, today=today),
        skips=_skips(status, listing),
        today=_today(figures),
        banner=(
            ""
            if connected
            else _("Anchor cannot reach its engine. These numbers may be out of date.")
        ),
    )


# -- the session ---------------------------------------------------------


def _card(status: dict[str, Any], stats: dict[str, Any], apps: dict[str, str]) -> SessionCard:
    level = str(status.get("level", Level.SOFT))
    remaining = float(status.get("remaining_seconds", 0))
    rest = status.get("break") if isinstance(status.get("break"), dict) else None

    return SessionCard(
        profile=str(status.get("profile", "")),
        level=_level_name(level),
        started=_("Started at {time}").format(time=_clock(status.get("started_at"))),
        countdown=format_countdown(remaining),
        remaining=_("remaining · ends at {time}").format(time=_clock(status.get("ends_at"))),
        break_title=_break_title(status, rest),
        break_when=_break_when(rest),
        break_detail=_break_detail(rest),
        attempts=str(int(status.get("blocked_attempts", 0))),
        attempts_detail=_refused(stats, apps),
        exit_how=_exit_how(status),
        exit_rule=_exit_rule(level),
        exit_pending=_exit_pending(status),
        actions=_actions(status),
    )


def _level_name(level: str) -> str:
    return {
        str(Level.SOFT): _("Soft"),
        str(Level.FIRM): _("Firm"),
        str(Level.STRICT): _("Strict"),
    }.get(level, level.capitalize())


def _clock(timestamp: Any) -> str:
    try:
        return datetime.fromtimestamp(float(timestamp)).strftime("%H:%M")
    except (TypeError, ValueError, OSError, OverflowError):
        return "?"


def _break_title(status: dict[str, Any], rest: dict[str, Any] | None) -> str:
    resting = str(status.get("phase", "")) == str(SessionPhase.BREAK) or (
        rest is not None and str(rest.get("phase", "")) == str(SessionPhase.BREAK)
    )
    if resting:
        return _("On a break")
    return _("Next break")


def _break_when(rest: dict[str, Any] | None) -> str:
    if rest is None:
        return _("No breaks in this profile")
    seconds = float(rest.get("remaining_seconds", 0))
    if str(rest.get("phase", "")) == str(SessionPhase.BREAK):
        return _("{duration} left").format(duration=_roughly(seconds))
    return _("in {duration}").format(duration=_roughly(seconds))


def _roughly(seconds: float) -> str:
    """Minutes, once there are minutes to speak of.

    "in 49 min 56 s" is a number pretending to be useful: nobody plans the
    next hour to the second, and a line that changes every second is one the
    eye learns to skip. Under a minute the seconds are the whole story.
    """
    if seconds < 60:
        return format_duration(seconds)
    return format_duration(round(seconds / 60) * 60)


def _break_detail(rest: dict[str, Any] | None) -> str:
    """``10 min · fullscreen``, as the mockup writes it."""
    if rest is None:
        return ""
    kind = str(rest.get("type", "")) or None
    shown = {
        str(BreakType.OVERLAY): _("fullscreen"),
        str(BreakType.NOTIFICATION): _("notification"),
    }.get(kind or "", "")
    if str(rest.get("phase", "")) == str(SessionPhase.BREAK):
        return shown
    length = rest.get("break_seconds")
    if length is None:
        return shown
    return (
        f"{format_duration(float(length))} · {shown}" if shown else format_duration(float(length))
    )


def _refused(stats: dict[str, Any], apps: dict[str, str]) -> str:
    """What is behind the count, as the mockup names it.

    Read from the statistics rather than kept in the session: SPEC 13 makes
    that database the one place a domain is written down, and the interface
    reading from it is not a second copy.

    An application is named the way its menu entry names it. ``discord.desktop``
    is Anchor's identifier for it, not a thing anyone recognises on a line
    that otherwise holds websites.
    """
    targets = stats.get("attempts_by_target") or []
    names: list[str] = []
    for entry in targets[:NAMED_TARGETS]:
        target = str(entry.get("target", ""))
        if str(entry.get("kind", "")) == "app":
            target = apps.get(target, target.removesuffix(".desktop").capitalize())
        if target:
            names.append(target)
    return ", ".join(names)


def _exit_how(status: dict[str, Any]) -> str:
    level = str(status.get("level", Level.SOFT))
    if level == str(Level.SOFT):
        return _("Wait 5 minutes")
    if level == str(Level.FIRM):
        return _("Wait + text")

    valve = str(status.get("valve") or "")
    return {
        str(Valve.WAIT): _("30-minute wait"),
        str(Valve.PHRASE): _("Random phrase"),
        str(Valve.BOTH): _("Wait and phrase"),
    }.get(valve, _("Emergency valve"))


def _exit_rule(level: str) -> str:
    if level == str(Level.SOFT):
        return _("Soft level rule. Lists stay editable.")
    if level == str(Level.FIRM):
        return _("Firm level rule. You may only add to the lists.")
    return _("Strict level rule. It cannot be cancelled; the valve is the only way out.")


def _exit_pending(status: dict[str, Any]) -> str:
    pending = status.get("exit_request")
    if not isinstance(pending, dict):
        return ""
    waiting = float(pending.get("remaining_wait_seconds", 0))
    if waiting > 0:
        return _("Leaving in {duration}. You can still withdraw it.").format(
            duration=format_duration(waiting)
        )
    if pending.get("phrase"):
        return _("Type the phrase to finish leaving.")
    return _("The wait is over.")


def _actions(status: dict[str, Any]) -> tuple[Action, ...]:
    level = str(status.get("level", Level.SOFT))
    origin = str(status.get("origin", SessionOrigin.MANUAL))
    pending = isinstance(status.get("exit_request"), dict)

    actions = [Action(label=_("Extend session"), request="session.extend", style="suggested")]

    if level == str(Level.STRICT):
        # SPEC 14: Home replaces "Cancel session" with "Emergency valve".
        actions.append(
            Action(label=_("Emergency valve"), request="valve.request", style="destructive")
        )
    else:
        actions.append(
            Action(label=_("Cancel session…"), request="session.cancel", style="destructive")
        )
        if origin == str(SessionOrigin.SCHEDULE):
            left = int(status.get("skips_remaining", 0))
            actions.append(
                Action(
                    label=_("Skip this session"),
                    request="schedule.skip",
                    enabled=left > 0,
                    hint=(
                        "" if left > 0 else _("No skips left this week. They come back on Monday.")
                    ),
                )
            )

    if pending:
        actions.append(Action(label=_("Withdraw"), request="session.withdraw_cancel"))
    return tuple(actions)


def _idle() -> IdleCard:
    return IdleCard(
        title=_("Nothing is blocked right now"),
        detail=_("Start a session, or let a schedule start one for you."),
        action=Action(label=_("Start session"), request="session.start", style="suggested"),
    )


# -- the rest of the screen ----------------------------------------------


#: Monday first, as the week is drawn everywhere in Anchor.
_DAYS = (
    N_("Monday"),
    N_("Tuesday"),
    N_("Wednesday"),
    N_("Thursday"),
    N_("Friday"),
    N_("Saturday"),
    N_("Sunday"),
)


def _upcoming(listing: dict[str, Any], *, today: int | None = None) -> tuple[ScheduleLine, ...]:
    entries = [entry for entry in listing.get("schedules", []) if entry.get("enabled", True)]
    weekday = datetime.now().weekday() if today is None else today
    # What is running now, then what runs today, then the rest by start time.
    # Sorting by start time alone puts Saturday's ten o'clock above this
    # afternoon's four, which is not what "coming up" means.
    entries.sort(
        key=lambda entry: (
            not entry.get("active"),
            weekday not in list(entry.get("days") or ()),
            entry.get("start", ""),
        )
    )

    lines: list[ScheduleLine] = []
    for entry in entries[:UPCOMING]:
        lines.append(
            ScheduleLine(
                when=_when(entry, weekday),
                name=str(entry.get("name", "")),
                level=_level_name(str(entry.get("level", Level.SOFT))),
                now=bool(entry.get("active")),
            )
        )
    return tuple(lines)


def _when(entry: dict[str, Any], today: int | None = None) -> str:
    """``Today · 16:00–19:00``, or the first weekday it runs."""
    window = f"{entry.get('start', '')}–{entry.get('end', '')}"
    if entry.get("active"):
        return _("Now · {window}").format(window=window)

    days = list(entry.get("days") or ())
    today = datetime.now().weekday() if today is None else today
    if today in days:
        return _("Today · {window}").format(window=window)
    if not days:
        return window
    following = min((day for day in days if day > today), default=min(days))
    return f"{_(_DAYS[following])} · {window}"


def _skips(status: dict[str, Any], listing: dict[str, Any]) -> str:
    remaining = listing.get("skips_remaining", status.get("skips_remaining"))
    if remaining is None:
        return ""
    total = 3
    return _("Skips this week: {used} of {total}").format(used=total - int(remaining), total=total)


def _today(stats: dict[str, Any]) -> tuple[Figure, ...]:
    breaks = stats.get("breaks") or {}
    return (
        Figure(value=format_duration(float(stats.get("focus_seconds", 0))), label=_("of focus")),
        Figure(value=str(int(breaks.get("taken", 0))), label=_("breaks taken")),
        Figure(value=str(int(stats.get("blocked_attempts", 0))), label=_("blocked attempts")),
    )
