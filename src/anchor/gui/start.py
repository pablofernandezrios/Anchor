"""Start Session, and the dialog that asks whether you meant it (SPEC 7.1).

The mockups draw two screens: one with the profile's defaults already filled
in — duration, level, valve, breaks — and one that reads the consequences back
before anything is enforced.

The confirmation is the part worth taking seriously. SPEC 7.1 lists what it
must say: when the session ends, how to get out of it, whether VPN and Tor go
with it, and which applications are about to be closed. Anchor is deliberately
hard to leave, so the last honest moment to change your mind is here, and a
dialog that said "Are you sure?" and nothing else would be throwing that
moment away.

Everything the form refuses, it refuses with a reason and before the engine is
asked. The engine refuses the same things again — it is the only thing that
decides anything (SPEC 5.1) — but a button that is greyed out with "a Strict
session needs a valve" underneath beats a dialog that appears after the click
to say the same words.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from anchor.cli.durations import format_duration
from anchor.gui.i18n import _
from anchor.protocol.messages import MAX_MANUAL_DURATION_SECONDS
from anchor.protocol.types import BreakHardness, BreakType, Level, Valve, WebMode

#: What one press of + or - is worth. Fifteen minutes is the smallest change
#: anybody makes to a focus session on purpose.
DURATION_STEP: Final = 15 * 60

#: The shortest session the form will offer.
MIN_DURATION: Final = DURATION_STEP

#: SPEC 7.1: eight hours at start, for manual sessions. Schedules are exempt.
MAX_MANUAL: Final = MAX_MANUAL_DURATION_SECONDS

#: SPEC 7.1: what the applications already open get before they are closed.
GRACE_MINUTES: Final = 2


@dataclass(frozen=True, slots=True)
class Choice:
    """One option on a card: what it is called and what it costs."""

    key: str
    title: str
    detail: str


@dataclass(frozen=True, slots=True)
class StartForm:
    """Everything on the Start Session screen (mockup 2)."""

    profiles: tuple[str, ...]
    profile: str
    blocks: str

    duration_seconds: int
    duration: str
    ends: str
    cap: str

    levels: tuple[Choice, ...]
    level: str

    valves: tuple[Choice, ...]
    valve: str

    breaks: str
    breaks_hint: str

    can_start: bool = True
    problem: str = ""

    block_vpn_and_tor: bool = True
    web_mode: str = str(WebMode.BLOCKLIST)
    closing: tuple[str, ...] = ()
    """The applications this profile closes, by the name a person knows."""


@dataclass(frozen=True, slots=True)
class Confirmation:
    """The dialog SPEC 7.1 requires, in the mockup's words (mockup 3)."""

    title: str
    profile_line: str
    ends_line: str
    exit_line: str
    tunnels_line: str = ""
    apps_line: str = ""
    web_line: str = ""
    confirm: str = ""
    back: str = ""


def stepped(seconds: int, direction: int) -> int:
    """One press of + or -, held inside what a session may be."""
    moved = seconds + direction * DURATION_STEP
    return max(MIN_DURATION, min(MAX_MANUAL, moved))


def start_form(
    *,
    profiles: list[str],
    profile: dict[str, Any],
    duration_seconds: int,
    level: str = str(Level.FIRM),
    valve: str | None = None,
    now: float,
    apps: dict[str, str] | None = None,
) -> StartForm:
    """The screen, filled in from a profile and what the user has changed."""
    names = apps or {}
    breaks = profile.get("breaks") or {}
    blocked_apps = list(profile.get("apps") or ())

    problem = _problem(duration_seconds=duration_seconds, level=level, valve=valve)
    return StartForm(
        profiles=tuple(profiles),
        profile=str(profile.get("name", "")),
        blocks=_blocks(profile),
        duration_seconds=duration_seconds,
        duration=format_duration(duration_seconds),
        ends=_ends(now, duration_seconds),
        cap=_("Maximum at start: {duration}").format(duration=format_duration(MAX_MANUAL)),
        levels=_levels(),
        level=level,
        valves=_valves() if level == str(Level.STRICT) else (),
        valve=valve or "",
        breaks=_breaks(breaks),
        breaks_hint=_("Change it in the profile"),
        can_start=not problem,
        problem=problem,
        block_vpn_and_tor=bool(profile.get("block_vpn_and_tor", True)),
        web_mode=str(profile.get("web_mode", WebMode.BLOCKLIST)),
        closing=tuple(names.get(app, app) for app in blocked_apps),
    )


def request_for(form: StartForm) -> tuple[str, dict[str, Any]]:
    """The request the button sends.

    The valve is left out below Strict rather than sent as null: it has no
    meaning there, and the engine's schema refuses a key that is present and
    empty.
    """
    payload: dict[str, Any] = {
        "profile": form.profile,
        "duration_seconds": form.duration_seconds,
        "level": form.level,
    }
    if form.level == str(Level.STRICT) and form.valve:
        payload["valve"] = form.valve
    return "session.start", payload


# -- the form ------------------------------------------------------------


def _blocks(profile: dict[str, Any]) -> str:
    """``Blocklist · 4 categories · 2 domains · 2 apps``, as the mockup writes it."""
    mode = str(profile.get("web_mode", WebMode.BLOCKLIST))
    named = _("Allowlist") if mode == str(WebMode.ALLOWLIST) else _("Blocklist")
    categories = len(profile.get("categories") or ())
    domains = len(profile.get("domains") or ())
    apps = len(profile.get("apps") or ())
    return "{mode} · {categories} · {domains} · {apps}".format(
        mode=named,
        categories=(
            _("1 category") if categories == 1 else _("{n} categories").format(n=categories)
        ),
        domains=_("1 domain") if domains == 1 else _("{n} domains").format(n=domains),
        apps=_("1 app") if apps == 1 else _("{n} apps").format(n=apps),
    )


def _ends(now: float, duration_seconds: int) -> str:
    try:
        started = datetime.fromtimestamp(now)
        finishes = datetime.fromtimestamp(now + duration_seconds)
    except (OverflowError, OSError, ValueError):
        return ""

    clock = finishes.strftime("%H:%M")
    days = (finishes.date() - started.date()).days
    if days == 0:
        return _("Ends today at {time}").format(time=clock)
    if days == 1:
        return _("Ends tomorrow at {time}").format(time=clock)
    return _("Ends on {date} at {time}").format(date=finishes.strftime("%-d %B"), time=clock)


def _levels() -> tuple[Choice, ...]:
    """SPEC 7.2's table, in the mockup's words."""
    return (
        Choice(
            key=str(Level.SOFT),
            title=_("Soft"),
            detail=_("Cancel after a 5 min wait. Lists stay editable."),
        ),
        Choice(
            key=str(Level.FIRM),
            title=_("Firm"),
            detail=_("Cancel with a long wait and a typed text. You may only add sites."),
        ),
        Choice(
            key=str(Level.STRICT),
            title=_("Strict"),
            detail=_("No cancelling. The valve is the only way out. Blocks VPN and Tor."),
        ),
    )


def _valves() -> tuple[Choice, ...]:
    """SPEC 7.5's three, with what each one actually feels like."""
    return (
        Choice(
            key=str(Valve.WAIT),
            title=_("30-minute wait"),
            detail=_("You can change your mind and withdraw the request."),
        ),
        Choice(
            key=str(Valve.PHRASE),
            title=_("Random phrase"),
            detail=_("Anchor generates a new long phrase every time."),
        ),
        Choice(key=str(Valve.BOTH), title=_("Both"), detail=_("The wait, and then the phrase.")),
    )


def _breaks(breaks: dict[str, Any]) -> str:
    """``50/10 · Moderate · Fullscreen``, plus the long break if there is one."""
    if not breaks:
        return _("No breaks")

    work = int(breaks.get("work_minutes", 50))
    rest = int(breaks.get("break_minutes", 10))
    hardness = {
        str(BreakHardness.FLEXIBLE): _("Flexible"),
        str(BreakHardness.MODERATE): _("Moderate"),
        str(BreakHardness.MANDATORY): _("Mandatory"),
    }.get(str(breaks.get("hardness", "")), "")
    kind = {
        str(BreakType.OVERLAY): _("Fullscreen"),
        str(BreakType.NOTIFICATION): _("Notification"),
    }.get(str(breaks.get("type", "")), "")

    line = f"{work}/{rest} · {hardness} · {kind}"
    every = breaks.get("long_break_every")
    minutes = breaks.get("long_break_minutes")
    if every and minutes:
        line += " · " + _("{minutes} min every {cycles} cycles").format(
            minutes=int(minutes), cycles=int(every)
        )
    return line


def _problem(*, duration_seconds: int, level: str, valve: str | None) -> str:
    if duration_seconds > MAX_MANUAL:
        return _("A session started by hand can be at most {duration}.").format(
            duration=format_duration(MAX_MANUAL)
        )
    if duration_seconds < MIN_DURATION:
        return _("A session must be at least {duration}.").format(
            duration=format_duration(MIN_DURATION)
        )
    if level == str(Level.STRICT) and not valve:
        return _("A Strict session needs a valve, because it cannot be cancelled.")
    return ""


# -- the confirmation ----------------------------------------------------


def confirmation(form: StartForm) -> Confirmation:
    """What the dialog says before anything is enforced (SPEC 7.1)."""
    level = {
        str(Level.SOFT): _("Soft"),
        str(Level.FIRM): _("Firm"),
        str(Level.STRICT): _("Strict"),
    }.get(form.level, form.level)

    return Confirmation(
        title=_("Start a {level} session?").format(level=level),
        profile_line=_("{profile} profile · {duration}").format(
            profile=form.profile, duration=form.duration
        ),
        ends_line=_("{ends}. You can extend it, never shorten it.").format(ends=form.ends),
        exit_line=_exit_line(form),
        tunnels_line=_tunnels_line(form),
        apps_line=_apps_line(form),
        web_line=_web_line(form),
        confirm=_("Start session"),
        back=_("Back"),
    )


def _exit_line(form: StartForm) -> str:
    if form.level == str(Level.SOFT):
        return _("You can cancel it after waiting 5 minutes.")
    if form.level == str(Level.FIRM):
        return _("Cancelling means a long wait and typing a long random text.")

    named = {
        str(Valve.WAIT): _("a 30-minute wait"),
        str(Valve.PHRASE): _("a random phrase"),
        str(Valve.BOTH): _("a 30-minute wait and then a random phrase"),
    }.get(form.valve, _("the emergency valve"))
    return _("It cannot be cancelled. The only way out is {valve}.").format(valve=named)


def _tunnels_line(form: StartForm) -> str:
    """Only when it is true: ADR 4 lets a profile keep its tunnels."""
    if form.level != str(Level.STRICT) or not form.block_vpn_and_tor:
        return ""
    return _("VPN and Tor are blocked for the whole session.")


def _apps_line(form: StartForm) -> str:
    if not form.closing:
        return ""
    return _("{apps} will close in {minutes} minutes. Save your work.").format(
        apps=_and_list(form.closing), minutes=GRACE_MINUTES
    )


def _web_line(form: StartForm) -> str:
    """SPEC 8.1 asks for a clear allowlist warning, and this is the moment."""
    if form.web_mode != str(WebMode.ALLOWLIST):
        return ""
    return _(
        "This profile allows a list and blocks everything except it. Many sites "
        "load parts of themselves from other domains and will need entries of "
        "their own."
    )


def _and_list(items: tuple[str, ...]) -> str:
    """``Discord and Steam``; ``Discord, Steam and Telegram``."""
    if len(items) == 1:
        return items[0]
    return _("{first} and {last}").format(first=", ".join(items[:-1]), last=items[-1])
