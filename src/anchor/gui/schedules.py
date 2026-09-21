"""The Schedules screen: a week, drawn (SPEC 11, mockup 5).

The mockup draws seven columns and a time axis, with each schedule as a block
sitting where it runs. Turning a list of schedules into that picture is
arithmetic, and it is done here so that the GTK layer only places rectangles.

The case a naive version loses is the window that crosses midnight, which
SPEC 11 allows. ``22:00–02:00`` on a Monday is not one block: it is two, one
running to midnight on Monday and one from midnight on Tuesday, and a Sunday
night window wraps round onto Monday. Both halves carry the whole window in
their label, so that neither reads as a schedule that stops at midnight.

What can be done to a schedule is the other half of the screen. SPEC 11 lets
them be edited and deleted freely until they start, and after that only
skipped — three a week, reset on Monday, and never for a Strict one. Every
refusal is shown as a disabled control with the reason beside it rather than
as an error after the click.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Final

from anchor.gui.i18n import _
from anchor.protocol.types import Level, SessionOrigin

MINUTES_IN_A_DAY: Final = 24 * 60

#: Skips a week (SPEC 11). Held here too so the screen can say "2 of 3".
SKIPS_PER_WEEK: Final = 3

#: What the axis shows when there is nothing to show: an ordinary working day.
EMPTY_AXIS: Final = (8 * 60, 20 * 60)

#: Monday first, as SPEC 11 counts the week.
DAY_NAMES: Final = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
DAY_KEYS: Final = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


@dataclass(frozen=True, slots=True)
class Block:
    """One schedule, on one day, in the grid."""

    id: str
    name: str
    level: str
    window: str
    """The whole window, both halves of a midnight crossing included."""

    from_minute: int
    to_minute: int
    offset: float
    """Where the block starts, as a fraction of the axis."""

    extent: float
    """How much of the axis it covers."""

    active: bool = False
    enabled: bool = True
    editable: bool = True
    reason: str = ""


@dataclass(frozen=True, slots=True)
class DayColumn:
    """One column of the week."""

    index: int
    name: str
    today: bool = False
    blocks: tuple[Block, ...] = ()


@dataclass(frozen=True, slots=True)
class ScheduleGrid:
    """The whole screen (mockup 5)."""

    days: tuple[DayColumn, ...] = ()
    hours: tuple[str, ...] = ()
    first_minute: int = EMPTY_AXIS[0]
    last_minute: int = EMPTY_AXIS[1]

    skips: str = ""
    can_skip: bool = False
    skip_label: str = ""
    skip_reason: str = ""
    note: str = ""


def schedule_grid(
    *,
    listing: dict[str, Any],
    status: dict[str, Any],
    today: int,
) -> ScheduleGrid:
    """The week, from the engine's list of schedules."""
    entries = list(listing.get("schedules") or [])
    placed: list[tuple[int, Block]] = []
    for entry in entries:
        placed.extend(_pieces(entry))

    first, last = _axis([block for _day, block in placed])
    span = max(1, last - first)

    days = tuple(
        DayColumn(
            index=index,
            name=_(DAY_NAMES[index]),
            today=index == today,
            blocks=tuple(
                _within(block, first, span)
                for day, block in sorted(placed, key=lambda one: one[1].from_minute)
                if day == index
            ),
        )
        for index in range(7)
    )

    # The status wins over the listing: both carry the same number, but the
    # status arrives on every tick and the listing is a snapshot from when the
    # screen was opened, which may be a skip ago.
    remaining = int(status.get("skips_remaining", listing.get("skips_remaining", SKIPS_PER_WEEK)))
    refusal = _why_not_skip(status, remaining)
    return ScheduleGrid(
        days=days,
        hours=_hours(first, last),
        first_minute=first,
        last_minute=last,
        skips=_("Skips this week: {used} of {total}").format(
            used=SKIPS_PER_WEEK - remaining, total=SKIPS_PER_WEEK
        ),
        can_skip=not refusal,
        skip_label=_("Skip the running session ({left} left)").format(left=remaining),
        skip_reason=refusal,
        note=_(
            "Schedules are edited freely while they are not running. A running "
            "session can only be skipped, up to three times a week."
        ),
    )


# -- placing the blocks --------------------------------------------------


def _pieces(entry: dict[str, Any]) -> list[tuple[int, Block]]:
    """One schedule as the one or two blocks it draws on the week."""
    start = _minutes(str(entry.get("start", "00:00")))
    end = _minutes(str(entry.get("end", "00:00")))
    label = _window(start, end)
    days = [int(day) % 7 for day in entry.get("days") or ()]

    crosses = end <= start
    pieces: list[tuple[int, Block]] = []
    for day in days:
        if not crosses:
            pieces.append((day, _block(entry, start, end, label)))
            continue
        # SPEC 11: a window may run past midnight. It is two blocks, and the
        # second belongs to the next day — which for a Sunday is Monday.
        pieces.append((day, _block(entry, start, MINUTES_IN_A_DAY, label)))
        pieces.append(((day + 1) % 7, _block(entry, 0, end, label)))
    return pieces


def _block(entry: dict[str, Any], start: int, end: int, label: str) -> Block:
    active = bool(entry.get("active"))
    return Block(
        id=str(entry.get("id", "")),
        name=str(entry.get("name", "")),
        level=str(entry.get("level", Level.SOFT)),
        window=label,
        from_minute=start,
        to_minute=end,
        offset=0.0,
        extent=0.0,
        active=active,
        enabled=bool(entry.get("enabled", True)),
        editable=not active,
        reason=(
            _("This schedule is running now. It can be skipped, not changed.") if active else ""
        ),
    )


def _within(block: Block, first: int, span: int) -> Block:
    """The same block, told where it sits in the axis."""
    return replace(
        block,
        offset=(block.from_minute - first) / span,
        extent=(block.to_minute - block.from_minute) / span,
    )


def _axis(blocks: list[Block]) -> tuple[int, int]:
    if not blocks:
        return EMPTY_AXIS
    first = min(block.from_minute for block in blocks)
    last = max(block.to_minute for block in blocks)
    # Whole hours, so the labels line up with the lines.
    first -= first % 60
    if last % 60:
        last += 60 - last % 60
    return first, max(last, first + 60)


def _hours(first: int, last: int) -> tuple[str, ...]:
    return tuple(f"{minute // 60:02d}:00" for minute in range(first, last + 1, 60))


def _minutes(clock: str) -> int:
    try:
        hours, minutes = clock.split(":")
        return int(hours) * 60 + int(minutes)
    except (ValueError, AttributeError):
        return 0


def _window(start: int, end: int) -> str:
    """``9–13``, or ``9:30–13:15`` when the minutes matter, as the mockup does."""
    return f"{_short(start)}–{_short(end)}"


def _short(minute: int) -> str:
    hours, minutes = divmod(minute % MINUTES_IN_A_DAY, 60)
    return f"{hours}:{minutes:02d}" if minutes else str(hours)


# -- skipping ------------------------------------------------------------


def _why_not_skip(status: dict[str, Any], remaining: int) -> str:
    """The reason the skip button is disabled, or nothing (SPEC 11)."""
    if not status.get("active"):
        return _("Nothing is running.")
    if str(status.get("origin", SessionOrigin.MANUAL)) != str(SessionOrigin.SCHEDULE):
        return _("This session was started by hand. Skipping is for scheduled ones.")
    if str(status.get("level", Level.SOFT)) == str(Level.STRICT):
        return _("A Strict scheduled session cannot be skipped. Only its valve applies.")
    if remaining <= 0:
        return _("No skips left this week. They come back on Monday.")
    return ""


# -- saving --------------------------------------------------------------


def create_request(
    *,
    name: str,
    profile: str,
    days: list[int],
    start: str,
    end: str,
    level: str,
    valve: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """A new schedule, in the shape ``schedule.create`` takes."""
    payload: dict[str, Any] = {
        "name": name,
        "profile": profile,
        "days": [DAY_KEYS[day % 7] for day in sorted(days)],
        "start": start,
        "end": end,
        "level": level,
    }
    # Left out rather than sent empty: the schema refuses a key that is
    # present and null.
    if valve:
        payload["valve"] = valve
    return "schedule.create", payload


def edit_request(
    before: dict[str, Any], after: dict[str, Any]
) -> tuple[str, dict[str, Any]] | None:
    """Only what moved, or ``None`` when nothing did."""
    payload: dict[str, Any] = {"id": str(before.get("id", ""))}

    for key in ("name", "profile", "start", "end", "level", "valve"):
        if before.get(key) != after.get(key) and after.get(key) is not None:
            payload[key] = after[key]

    if sorted(before.get("days") or ()) != sorted(after.get("days") or ()):
        payload["days"] = [DAY_KEYS[int(day) % 7] for day in sorted(after.get("days") or ())]

    if before.get("enabled", True) != after.get("enabled", True):
        payload["enabled"] = bool(after.get("enabled", True))

    return ("schedule.edit", payload) if len(payload) > 1 else None


def delete_request(identifier: str) -> tuple[str, dict[str, Any]]:
    return "schedule.delete", {"id": identifier}
