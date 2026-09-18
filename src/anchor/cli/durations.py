"""Reading and writing durations the way people say them (SPEC 15).

``--duration 2h30m`` is what the specification shows, so the parser accepts
that shape and its obvious relatives: ``90m``, ``2h``, ``45s``, ``1h 30m``.
A bare number is read as minutes, because that is what someone typing
``--duration 90`` almost certainly means.
"""

from __future__ import annotations

import re
from typing import Final

_UNITS: Final[dict[str, int]] = {"h": 3600, "m": 60, "s": 1}
_PART = re.compile(r"(\d+)\s*([hms])", re.IGNORECASE)


class DurationError(ValueError):
    """The text is not a duration Anchor understands."""


def parse_duration(text: str) -> int:
    """Return ``text`` as a whole number of seconds."""
    cleaned = text.strip().lower()
    if not cleaned:
        raise DurationError("a duration cannot be empty")

    if cleaned.isdigit():
        minutes = int(cleaned)
        if minutes <= 0:
            raise DurationError("a duration must be greater than zero")
        return minutes * 60

    matches = list(_PART.finditer(cleaned))
    if not matches:
        raise DurationError(f"{text!r} is not a duration; try 2h30m, 90m or 45s")

    # Reject trailing rubbish such as "2h30x", which the regex would ignore.
    consumed = sum(match.end() - match.start() for match in matches)
    if len(re.sub(r"\s+", "", cleaned)) != consumed - cleaned.count(" "):
        leftover = _PART.sub("", cleaned).strip()
        if leftover:
            raise DurationError(f"{text!r} has trailing characters: {leftover!r}")

    total = 0
    seen: set[str] = set()
    for match in matches:
        unit = match.group(2)
        if unit in seen:
            raise DurationError(f"{text!r} repeats the {unit!r} unit")
        seen.add(unit)
        total += int(match.group(1)) * _UNITS[unit]

    if total <= 0:
        raise DurationError("a duration must be greater than zero")
    return total


def format_duration(seconds: float) -> str:
    """Render ``seconds`` the way the interface shows it: ``2 h 30 min``."""
    total = int(max(0, seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} h {minutes} min" if minutes else f"{hours} h"
    if minutes:
        return f"{minutes} min" if not secs else f"{minutes} min {secs} s"
    return f"{secs} s"


def format_countdown(seconds: float) -> str:
    """Render ``seconds`` as the clock the interface counts down: ``2:14:37``."""
    total = int(max(0, seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"
