"""The Statistics screen (SPEC 13, mockup 6).

Four headline numbers, a bar per day, what was refused most often, and the
break and rupture counts. The engine has already summed everything up, so this
turns one summary into the words and proportions the page draws.

Two decisions worth stating.

**A count that never happened is still shown.** A rupture kind with no row
reads as "not measured", which is a different claim from zero — and on a page
whose whole job is to be honest about how a fortnight actually went, the
difference matters.

**Applications are marked as applications.** ``Discord`` and ``discord.com``
are two different things to block, and a ranked list that mixed them without
saying which was which would be unreadable at exactly the moment it is most
interesting.

SPEC 13 also puts the one-action deletion here, with what it costs written
next to it: there is no copy to restore from, because a private record that
survives its own deletion is not private.
"""

from __future__ import annotations

import locale
from dataclasses import dataclass
from datetime import date
from typing import Any, Final

from anchor.cli.durations import format_duration
from anchor.gui.i18n import N_, _
from anchor.gui.schedules import DAY_NAMES
from anchor.protocol.types import RuptureKind

#: The ranges SPEC 13 asks for.
RANGES: Final = ("day", "week", "month")


@dataclass(frozen=True, slots=True)
class Choice:
    key: str
    title: str


@dataclass(frozen=True, slots=True)
class Figure:
    """One number with what it counts underneath."""

    value: str
    label: str


@dataclass(frozen=True, slots=True)
class Bar:
    """One day of the focus chart."""

    label: str
    value: str
    fraction: float


@dataclass(frozen=True, slots=True)
class Row:
    """One line of the ranked list of refusals."""

    target: str
    label: str
    count: int
    fraction: float
    kind: str = "domain"


@dataclass(frozen=True, slots=True)
class StatsView:
    """The whole screen (mockup 6)."""

    range: str = "week"
    ranges: tuple[Choice, ...] = ()
    period: str = ""

    headline: tuple[Figure, ...] = ()
    bars: tuple[Bar, ...] = ()
    bars_title: str = ""

    attempts: tuple[Row, ...] = ()
    attempts_title: str = ""
    attempts_empty: str = ""

    breaks: tuple[Figure, ...] = ()
    breaks_title: str = ""
    ruptures: tuple[Figure, ...] = ()
    ruptures_title: str = ""

    delete_label: str = ""
    delete_warning: str = ""


def stats_view(*, summary: dict[str, Any], apps: dict[str, str] | None = None) -> StatsView:
    """One summary from the engine, as a page."""
    names = apps or {}
    view = str(summary.get("view", "week"))

    return StatsView(
        range=view,
        ranges=(
            Choice(key="day", title=_("Day")),
            Choice(key="week", title=_("Week")),
            Choice(key="month", title=_("Month")),
        ),
        period=_period(str(summary.get("first_day", "")), str(summary.get("last_day", ""))),
        headline=(
            Figure(
                value=format_duration(float(summary.get("focus_seconds", 0))),
                label=_("Focus hours"),
            ),
            Figure(
                value=str(int(summary.get("sessions_completed", 0))), label=_("Sessions completed")
            ),
            Figure(value=str(int(summary.get("blocked_attempts", 0))), label=_("Blocked attempts")),
            Figure(value=str(int(summary.get("ruptures", 0))), label=_("Ruptures")),
        ),
        bars=_bars(summary.get("focus_by_day") or [], monthly=view == "month"),
        bars_title=_("Focus hours per day"),
        attempts=_attempts(summary.get("attempts_by_target") or [], names),
        attempts_title=_("Blocked attempts by domain"),
        attempts_empty=_("Nothing was refused in this period."),
        breaks=_breaks(summary.get("breaks") or {}),
        breaks_title=_("Breaks"),
        ruptures=_ruptures(summary.get("ruptures_by_kind") or {}),
        ruptures_title=_("Ruptures"),
        delete_label=_("Delete all statistics"),
        delete_warning=_(
            "This deletes everything Anchor has recorded. It cannot be undone: "
            "Anchor keeps no copy, because a private record that survives its "
            "own deletion is not private."
        ),
    )


# -- the period ----------------------------------------------------------


def _period(first: str, last: str) -> str:
    """``21–27 September 2026``, or both months when it crosses one."""
    start = _date(first)
    end = _date(last)
    if start is None or end is None:
        return ""
    if start == end:
        return _("{day} {month} {year}").format(day=start.day, month=_month(start), year=start.year)
    if (start.month, start.year) == (end.month, end.year):
        return _("{first}–{last} {month} {year}").format(
            first=start.day, last=end.day, month=_month(end), year=end.year
        )
    if start.year == end.year:
        return _("{first} {first_month} – {last} {last_month} {year}").format(
            first=start.day,
            first_month=_month(start),
            last=end.day,
            last_month=_month(end),
            year=end.year,
        )
    return f"{_period(first, first)} – {_period(last, last)}"


def _date(text: str) -> date | None:
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


_MONTHS: Final = (
    N_("January"),
    N_("February"),
    N_("March"),
    N_("April"),
    N_("May"),
    N_("June"),
    N_("July"),
    N_("August"),
    N_("September"),
    N_("October"),
    N_("November"),
    N_("December"),
)


def _month(when: date) -> str:
    return _(_MONTHS[when.month - 1])


# -- the chart -----------------------------------------------------------


def _bars(days: list[dict[str, Any]], *, monthly: bool) -> tuple[Bar, ...]:
    if not days:
        return ()
    tallest = max(float(entry.get("seconds", 0)) for entry in days)

    bars: list[Bar] = []
    for entry in days:
        seconds = float(entry.get("seconds", 0))
        when = _date(str(entry.get("day", "")))
        bars.append(
            Bar(
                # Thirty weekday names in a row would say nothing at all, so a
                # month is labelled by the day of the month instead.
                label=(
                    str(when.day)
                    if monthly and when is not None
                    else (_(DAY_NAMES[when.weekday()]) if when is not None else "")
                ),
                value=_hours(seconds),
                # A day with no focus at all must not divide by zero.
                fraction=(seconds / tallest) if tallest > 0 else 0.0,
            )
        )
    return tuple(bars)


def _hours(seconds: float) -> str:
    """``3.5``, or ``3,5`` where the locale writes it that way."""
    hours = seconds / 3600
    try:
        return locale.format_string("%.1f", hours)
    except (ValueError, TypeError):  # pragma: no cover - a locale with no rules
        return f"{hours:.1f}"


# -- what was refused ----------------------------------------------------


def _attempts(targets: list[dict[str, Any]], names: dict[str, str]) -> tuple[Row, ...]:
    if not targets:
        return ()
    most = max(int(entry.get("count", 0)) for entry in targets) or 1

    rows: list[Row] = []
    for entry in targets:
        kind = str(entry.get("kind", "domain"))
        raw = str(entry.get("target", ""))
        shown = names.get(raw, raw) if kind == "app" else raw
        rows.append(
            Row(
                target=shown,
                label=_("{name} (app)").format(name=shown) if kind == "app" else shown,
                count=int(entry.get("count", 0)),
                fraction=int(entry.get("count", 0)) / most,
                kind=kind,
            )
        )
    return tuple(rows)


# -- the counters --------------------------------------------------------


def _breaks(counts: dict[str, Any]) -> tuple[Figure, ...]:
    return tuple(
        Figure(value=str(int(counts.get(key, 0))), label=label)
        for key, label in (
            ("taken", _("taken")),
            ("postponed", _("postponed")),
            ("skipped", _("skipped")),
        )
    )


def _ruptures(counts: dict[str, Any]) -> tuple[Figure, ...]:
    """Every kind, including the ones that never happened (SPEC 13)."""
    return tuple(
        Figure(value=str(int(counts.get(key, 0))), label=label)
        for key, label in (
            (str(RuptureKind.VALVE), _("valves used")),
            (str(RuptureKind.SKIP), _("schedules skipped")),
            (str(RuptureKind.TAMPERING), _("tampering")),
        )
    )
