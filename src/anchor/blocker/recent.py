"""A short-lived memory of what recently resolved (SPEC 8.2).

When a session starts, a browser may already have pages open to sites the
session blocks. Blocking DNS from that moment does nothing for them: the
address is already known and the connection already made. SPEC 8.2 asks for a
short-lived map of recent answers so those addresses can be rejected for the
duration.

This lives in memory and is never written anywhere. It holds names the user
looked up, which the working rules keep out of the log and out of everything
except the statistics database, so it is bounded, it expires, and it is emptied
when the session that needed it ends.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

#: How long an answer stays interesting. Long enough to catch what a browser
#: resolved just before a session began, short enough that the map is not a
#: record of the afternoon.
DEFAULT_TTL: Final = 600.0

#: A ceiling, so a machine resolving a great deal cannot grow this without end.
DEFAULT_MAX_ENTRIES: Final = 4096


@dataclass(frozen=True, slots=True)
class Answer:
    addresses: tuple[str, ...]
    at: float


class RecentAnswers:
    """Names resolved recently, and the addresses they resolved to."""

    def __init__(
        self,
        *,
        ttl: float = DEFAULT_TTL,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl
        self._max = max_entries
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, Answer] = OrderedDict()

    def record(self, name: str, addresses: Sequence[str]) -> None:
        """Remember that ``name`` resolved to ``addresses``."""
        if not addresses:
            return
        with self._lock:
            self._entries[name] = Answer(tuple(addresses), self._clock())
            self._entries.move_to_end(name)
            while len(self._entries) > self._max:
                self._entries.popitem(last=False)

    def addresses_for(self, blocked: Callable[[str], bool]) -> list[str]:
        """Addresses of every remembered name that ``blocked`` says to block.

        Called once when a session starts, to build the set of addresses the
        firewall should reject.
        """
        cutoff = self._clock() - self._ttl
        found: list[str] = []
        seen: set[str] = set()

        with self._lock:
            for name, answer in self._entries.items():
                if answer.at < cutoff or not blocked(name):
                    continue
                for address in answer.addresses:
                    if address not in seen:
                        seen.add(address)
                        found.append(address)
        return found

    def prune(self) -> None:
        """Forget anything past its time."""
        cutoff = self._clock() - self._ttl
        with self._lock:
            stale = [name for name, answer in self._entries.items() if answer.at < cutoff]
            for name in stale:
                del self._entries[name]

    def clear(self) -> None:
        """Forget everything. Called when a session ends."""
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)
