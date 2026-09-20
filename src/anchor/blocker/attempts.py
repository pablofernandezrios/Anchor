"""Counting blocked attempts, and deciding when to say something (SPEC 8.3).

Two different questions, with two different answers.

**How many attempts were there?** The number on the indicator and in the
statistics. It should mean "times I tried to go there", and the naive count
does not: resolving one name asks for its IPv4 and IPv6 addresses separately,
so a single visit arrives here twice, and a browser retrying turns one visit
into four or five. The end-to-end test in Milestone 2 showed exactly that,
every domain counted twice. So attempts to the same name inside a few seconds
are one attempt.

**Should the user be told?** At most one notification per domain every ten
minutes, as SPEC 8.3 requires. Someone who keeps reaching for a blocked site
should not be punished with a stream of notifications for it.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

#: One notification per domain per ten minutes (SPEC 8.3).
NOTIFY_INTERVAL: Final = 600.0

#: Queries for the same name closer together than this are one attempt.
#: Comfortably longer than the gap between the A and AAAA lookups a single
#: visit makes, and far shorter than a person deciding to try again.
ATTEMPT_WINDOW: Final = 5.0

#: A ceiling on how many domains are tracked at once.
MAX_TRACKED: Final = 2048


@dataclass(frozen=True, slots=True)
class Outcome:
    """What to do about one blocked query."""

    counted: bool
    """Whether this was a new attempt rather than part of the last one."""

    notify: bool
    """Whether to tell the user."""


class AttemptTracker:
    """Decides what counts as an attempt, and what deserves a notification."""

    def __init__(
        self,
        *,
        notify_interval: float = NOTIFY_INTERVAL,
        attempt_window: float = ATTEMPT_WINDOW,
        max_tracked: int = MAX_TRACKED,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._notify_interval = notify_interval
        self._attempt_window = attempt_window
        self._max = max_tracked
        self._clock = clock
        self._lock = threading.Lock()
        self._last_seen: OrderedDict[str, float] = OrderedDict()
        self._last_notified: OrderedDict[str, float] = OrderedDict()
        self._total = 0

    def record(self, domain: str) -> Outcome:
        """Register a blocked query and say what should follow from it."""
        now = self._clock()
        with self._lock:
            seen = self._last_seen.get(domain)
            counted = seen is None or (now - seen) >= self._attempt_window
            self._last_seen[domain] = now
            self._last_seen.move_to_end(domain)
            self._trim(self._last_seen)

            notify = False
            if counted:
                self._total += 1
                notified = self._last_notified.get(domain)
                if notified is None or (now - notified) >= self._notify_interval:
                    notify = True
                    self._last_notified[domain] = now
                    self._last_notified.move_to_end(domain)
                    self._trim(self._last_notified)

        return Outcome(counted=counted, notify=notify)

    def _trim(self, entries: OrderedDict[str, float]) -> None:
        while len(entries) > self._max:
            entries.popitem(last=False)

    @property
    def total(self) -> int:
        """Attempts counted since the session began."""
        with self._lock:
            return self._total

    def reset(self) -> None:
        """Start again. Called when a session ends."""
        with self._lock:
            self._last_seen.clear()
            self._last_notified.clear()
            self._total = 0
