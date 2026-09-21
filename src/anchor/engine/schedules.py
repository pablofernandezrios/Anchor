"""Weekly schedules: sessions that start without being asked (SPEC 11).

A schedule is days of the week plus a window, and the profile, level and valve
to run in it. The engine evaluates them on its own tick rather than through
systemd timers, because a timer that fires into a machine whose engine is
still starting has nowhere to put the session, and because the engine already
reconciles time across boots and suspends (SPEC 6.2).

Two rules here need more than restating:

* **A late boot joins the session in progress.** If the window opened while
  the machine was off, the session starts now and ends when the window does.
  Nothing is owed for the part that was missed: a schedule is a promise about
  a time of day, not a quota of hours.
* **Overlaps merge, and the strictest level wins.** That is one line in the
  specification and several decisions here; :func:`merge` writes them down.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from typing import Any, Self

from anchor.engine.profiles import Profile
from anchor.protocol.errors import AnchorError, ErrorCode
from anchor.protocol.types import Level, Valve, WebMode

#: Monday is 0, as ``date.weekday()`` has it.
DAY_NAMES: tuple[str, ...] = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)

MINUTES_IN_A_DAY = 24 * 60


def parse_day(value: str | int) -> int:
    """Turn ``"monday"`` or ``0`` into a weekday number."""
    if isinstance(value, int):
        if 0 <= value <= 6:
            return value
        raise ValueError(f"a weekday is 0 to 6, not {value}")
    name = value.strip().lower()
    if name in DAY_NAMES:
        return DAY_NAMES.index(name)
    raise ValueError(f"{value!r} is not a day of the week")


def parse_clock(value: str) -> int:
    """Turn ``"09:30"`` into minutes from midnight."""
    text = value.strip()
    parts = text.split(":")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        raise ValueError(f"{value!r} is not a time of day; try 09:30")
    hours, minutes = int(parts[0]), int(parts[1])
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        raise ValueError(f"{value!r} is not a time of day")
    return hours * 60 + minutes


def format_clock(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


@dataclass(frozen=True, slots=True)
class Schedule:
    """One weekly recurrence (SPEC 11)."""

    id: str
    name: str
    profile: str
    days: frozenset[int]
    start_minute: int
    end_minute: int
    level: Level = Level.SOFT
    valve: Valve | None = None
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("a schedule needs a name")
        if not self.days:
            raise ValueError("a schedule needs at least one day")
        if any(day < 0 or day > 6 for day in self.days):
            raise ValueError("a weekday is 0 to 6")
        for minute in (self.start_minute, self.end_minute):
            if not 0 <= minute < MINUTES_IN_A_DAY:
                raise ValueError("a time of day is between 00:00 and 23:59")
        if self.start_minute == self.end_minute:
            raise ValueError("a schedule's window cannot be empty")
        if self.level is Level.STRICT and self.valve is None:
            raise AnchorError(
                "a Strict schedule needs a valve, chosen now rather than later",
                code=ErrorCode.BAD_REQUEST,
            )

    @property
    def crosses_midnight(self) -> bool:
        """``22:00`` to ``02:00`` is a window, and a plausible one."""
        return self.end_minute < self.start_minute

    @property
    def length_minutes(self) -> int:
        if self.crosses_midnight:
            return MINUTES_IN_A_DAY - self.start_minute + self.end_minute
        return self.end_minute - self.start_minute

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "profile": self.profile,
            "days": sorted(self.days),
            "start": format_clock(self.start_minute),
            "end": format_clock(self.end_minute),
            "level": str(self.level),
            "valve": str(self.valve) if self.valve else None,
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Self:
        return cls(
            id=str(raw["id"]),
            name=str(raw["name"]),
            profile=str(raw["profile"]),
            days=frozenset(parse_day(day) for day in raw.get("days", ())),
            start_minute=parse_clock(str(raw["start"])),
            end_minute=parse_clock(str(raw["end"])),
            level=Level(raw.get("level", Level.SOFT)),
            valve=Valve(raw["valve"]) if raw.get("valve") else None,
            enabled=bool(raw.get("enabled", True)),
        )

    @classmethod
    def create(
        cls,
        *,
        name: str,
        profile: str,
        days: frozenset[int],
        start_minute: int,
        end_minute: int,
        level: Level = Level.SOFT,
        valve: Valve | None = None,
    ) -> Self:
        return cls(
            id=str(uuid.uuid4()),
            name=name,
            profile=profile,
            days=days,
            start_minute=start_minute,
            end_minute=end_minute,
            level=level,
            valve=valve,
        )


@dataclass(frozen=True, slots=True)
class Occurrence:
    """One run of one schedule, with the instants it covers."""

    schedule: Schedule
    started_at: float
    ends_at: float

    @property
    def id(self) -> str:
        return self.schedule.id

    def remaining(self, now: float) -> float:
        return max(0.0, self.ends_at - now)


def _midnight(day: date) -> datetime:
    return datetime.combine(day, time.min)


def occurrence_on(schedule: Schedule, day: date) -> Occurrence:
    """The run of ``schedule`` that begins on ``day``."""
    start = _midnight(day) + timedelta(minutes=schedule.start_minute)
    end = start + timedelta(minutes=schedule.length_minutes)
    return Occurrence(schedule=schedule, started_at=start.timestamp(), ends_at=end.timestamp())


def active_at(schedules: list[Schedule], now: float) -> list[Occurrence]:
    """Every schedule covering this instant (SPEC 11).

    Yesterday is considered as well as today, because a window that crosses
    midnight is still running in the small hours of the next day.
    """
    moment = datetime.fromtimestamp(now)
    found: list[Occurrence] = []
    for schedule in schedules:
        if not schedule.enabled:
            continue
        for offset in (0, 1):
            day = (moment - timedelta(days=offset)).date()
            if day.weekday() not in schedule.days:
                continue
            occurrence = occurrence_on(schedule, day)
            if occurrence.started_at <= now < occurrence.ends_at:
                found.append(occurrence)
                break
    return found


@dataclass(frozen=True, slots=True)
class Merged:
    """What overlapping schedules add up to (SPEC 11)."""

    profile: Profile
    level: Level
    valve: Valve | None
    ends_at: float
    schedule_ids: tuple[str, ...] = field(default_factory=tuple)


def merge(occurrences: list[Occurrence], profiles: dict[str, Profile]) -> Merged | None:
    """Combine overlapping schedules into one session (SPEC 11).

    "Rules are merged and the strictest level wins" is one line, and these are
    the readings it needs. Each one resolves towards *more* blocking, because
    a merge that could quietly unblock something would make two schedules
    weaker than one, which no user would expect.

    * The level is the strictest of them, and the valve comes with it.
    * Blocked domains, applications and categories are the union.
    * An allowlist beats a blocklist, because it blocks everything it does not
      name. Where two allowlists overlap, only what *both* allow stays
      allowed, and anything a blocklist in the same window names is taken out.
    * The session ends when the last of them ends. A schedule that ends
      earlier does not cut the others short.
    * Everything else — the break pattern, most of all — comes from the
      strictest schedule's profile, because that is the one whose rules the
      session is running under.
    """
    if not occurrences:
        return None

    named = [(occurrence, profiles.get(occurrence.schedule.profile)) for occurrence in occurrences]
    level = Level.strictest([occurrence.schedule.level for occurrence, _ in named])
    strictest = next(occurrence for occurrence, _ in named if occurrence.schedule.level is level)

    known = [
        (occurrence, profile)
        for occurrence, profile in sorted(
            named, key=lambda pair: -pair[0].schedule.level.strictness
        )
        if profile is not None
    ]
    if not known:
        # Every schedule in this window names a profile that is not installed.
        # Blocking nothing would be the wrong way to notice, so the session
        # runs on an allowlist of nothing, which is the strictest thing there
        # is, and the interface shows a profile it cannot find.
        merged = Profile(name=strictest.schedule.profile, web_mode=WebMode.ALLOWLIST)
    else:
        merged = _merge_profiles([profile for _occurrence, profile in known])

    return Merged(
        profile=merged,
        level=level,
        valve=strictest.schedule.valve,
        ends_at=max(occurrence.ends_at for occurrence in occurrences),
        schedule_ids=tuple(occurrence.id for occurrence in occurrences),
    )


def _merge_profiles(profiles: list[Profile]) -> Profile:
    if len(profiles) == 1:
        return profiles[0]

    allowlists = [profile for profile in profiles if profile.web_mode is WebMode.ALLOWLIST]
    blocklists = [profile for profile in profiles if profile.web_mode is WebMode.BLOCKLIST]

    apps: frozenset[str] = frozenset()
    categories: frozenset[str] = frozenset()
    for profile in profiles:
        apps |= profile.apps
        categories |= profile.categories

    if allowlists:
        allowed = allowlists[0].domains
        for profile in allowlists[1:]:
            allowed &= profile.domains
        for profile in blocklists:
            allowed -= profile.domains
        return replace(
            allowlists[0],
            name=" + ".join(profile.name for profile in profiles),
            web_mode=WebMode.ALLOWLIST,
            domains=allowed,
            apps=apps,
            categories=categories,
        )

    domains: frozenset[str] = frozenset()
    for profile in blocklists:
        domains |= profile.domains
    return replace(
        blocklists[0],
        name=" + ".join(profile.name for profile in profiles),
        web_mode=WebMode.BLOCKLIST,
        domains=domains,
        apps=apps,
        categories=categories,
        # One schedule asking for tunnels to be blocked is enough.
        block_vpn_and_tor=any(profile.block_vpn_and_tor for profile in profiles),
    )
