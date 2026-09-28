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

That rule is necessary and, on its own, badly insufficient. It counts per
domain, and the number of domains is not one: an allowlist session blocks
everything the machine reaches for, which on an idle desktop is telemetry,
update checks, captive-portal probes and a browser's own services -- hundreds
of distinct names nobody typed. The first run of an allowlist session on a
real desktop produced roughly four hundred notifications in five minutes,
each one obeying the per-domain rule perfectly, and left the machine crawling.

So there is a second limit, on the whole session rather than on each name: a
few notifications in any short window, and past that a single line saying how
many others there were. What the user needs to know is "Anchor is blocking
things", not the name of each one; the names are in the statistics, which is
where SPEC 13 says they belong.
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

#: How many notifications may be shown in one window, across every domain.
#: Three is enough to notice a pattern and few enough to ignore.
NOTIFY_BUDGET: Final = 3

#: The window the budget is spent over.
BUDGET_WINDOW: Final = 60.0


@dataclass(frozen=True, slots=True)
class Outcome:
    """What to do about one blocked query."""

    counted: bool
    """Whether this was a new attempt rather than part of the last one."""

    notify: bool
    """Whether to tell the user about this domain by name."""

    withheld: int = 0
    """How many notifications were held back while the budget was spent.

    Greater than zero only on the first notification of a new window, and
    then it is a digest rather than a domain: "and 47 other sites". A number
    the user can act on, instead of 47 things they cannot.
    """


class AttemptTracker:
    """Decides what counts as an attempt, and what deserves a notification."""

    def __init__(
        self,
        *,
        notify_interval: float = NOTIFY_INTERVAL,
        attempt_window: float = ATTEMPT_WINDOW,
        max_tracked: int = MAX_TRACKED,
        budget: int = NOTIFY_BUDGET,
        budget_window: float = BUDGET_WINDOW,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._notify_interval = notify_interval
        self._attempt_window = attempt_window
        self._max = max_tracked
        self._budget = budget
        self._budget_window = budget_window
        self._clock = clock
        self._lock = threading.Lock()
        self._last_seen: OrderedDict[str, float] = OrderedDict()
        self._last_notified: OrderedDict[str, float] = OrderedDict()
        self._total = 0
        self._window_started = 0.0
        self._spent = 0
        self._withheld = 0

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
            withheld = 0
            if counted:
                self._total += 1
                notified = self._last_notified.get(domain)
                if notified is None or (now - notified) >= self._notify_interval:
                    notify, withheld = self._afford(now)
                    if notify:
                        self._last_notified[domain] = now
                        self._last_notified.move_to_end(domain)
                        self._trim(self._last_notified)

        return Outcome(counted=counted, notify=notify, withheld=withheld)

    def _afford(self, now: float) -> tuple[bool, int]:
        """Whether the session's notification budget covers one more.

        Called with the lock held. Returns whether to notify, and how many
        were held back since the last one that got through -- which is what
        turns a silence into a sentence rather than a gap.
        """
        if now - self._window_started >= self._budget_window:
            self._window_started = now
            self._spent = 1
            withheld, self._withheld = self._withheld, 0
            return True, withheld

        if self._spent < self._budget:
            self._spent += 1
            return True, 0

        self._withheld += 1
        return False, 0

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
            self._window_started = 0.0
            self._spent = 0
            self._withheld = 0
