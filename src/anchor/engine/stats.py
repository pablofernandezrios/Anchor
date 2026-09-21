"""Statistics: what happened, kept locally and briefly (SPEC 13).

SQLite in ``/var/lib/anchor/stats.db``, written only by the engine, read by
whoever asks it. No telemetry, and nothing leaves the machine.

This is the one place where visited domains are written down. The working
rules forbid them everywhere else — they never reach the journal — and SPEC 13
limits even this to how often a name was asked for and how often it was
refused. There is no record of how long anything was looked at, because that
would be a record of a person's day rather than of Anchor's work.

Two decisions worth stating:

* **Focus is stored in day buckets**, split at local midnight when a session
  ends. A session from 23:00 to 01:00 is two hours of focus across two days,
  and saying so costs one small function; attributing it all to the day it
  began would quietly overstate one day and empty another.
* **Nothing here may end a session.** A statistic is a nice-to-have and a
  session is not, so every write is allowed to fail: it is logged, and the
  engine carries on (P4).
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger("anchord")

#: How long statistics are kept unless the settings say otherwise (SPEC 13).
DEFAULT_RETENTION_DAYS = 90

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id            TEXT PRIMARY KEY,
    profile       TEXT NOT NULL,
    level         TEXT NOT NULL,
    origin        TEXT NOT NULL,
    started_at    REAL NOT NULL,
    ended_at      REAL,
    reason        TEXT,
    completed     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS focus (
    day        TEXT NOT NULL,
    session_id TEXT,
    seconds    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS attempts (
    at         REAL NOT NULL,
    session_id TEXT,
    kind       TEXT NOT NULL,
    target     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS breaks (
    at         REAL NOT NULL,
    session_id TEXT,
    outcome    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ruptures (
    at         REAL NOT NULL,
    session_id TEXT,
    kind       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS focus_by_day ON focus (day);
CREATE INDEX IF NOT EXISTS attempts_by_time ON attempts (at);
CREATE INDEX IF NOT EXISTS breaks_by_time ON breaks (at);
CREATE INDEX IF NOT EXISTS ruptures_by_time ON ruptures (at);
"""


def split_by_day(started_at: float, ended_at: float) -> list[tuple[str, float]]:
    """Break a span into ``(YYYY-MM-DD, seconds)`` at local midnight.

    A session that runs past midnight belongs to both days, in the amounts it
    actually spent in each.
    """
    if ended_at <= started_at:
        return []

    buckets: list[tuple[str, float]] = []
    cursor = started_at
    while cursor < ended_at:
        moment = datetime.fromtimestamp(cursor)
        midnight = datetime.combine(moment.date() + timedelta(days=1), datetime.min.time())
        boundary = min(midnight.timestamp(), ended_at)
        buckets.append((moment.date().isoformat(), boundary - cursor))
        cursor = boundary
    return buckets


class Statistics:
    """The statistics database, and the only thing that writes to it."""

    def __init__(self, path: Path, *, retention_days: int = DEFAULT_RETENTION_DAYS) -> None:
        self.path = path
        self.retention_days = retention_days
        self._ready = False

    # -- the database ----------------------------------------------------

    def _open(self) -> sqlite3.Connection:
        """A connection with the schema in place. May raise."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        try:
            if not self._ready:
                connection.executescript(SCHEMA)
                self._ready = True
        except sqlite3.Error:
            connection.close()
            raise
        return connection

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """One short-lived connection, committed or rolled back as one."""
        connection = self._open()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @contextmanager
    def _writing(self, what: str) -> Iterator[sqlite3.Connection | None]:
        """Like ``_connect``, but a broken database never ends a session.

        A failure to *open* has to be handled apart from a failure to write:
        a context manager that raises before it yields takes the caller down
        with it, which is the opposite of the point.
        """
        try:
            connection = self._open()
        except (sqlite3.Error, OSError) as error:
            log.warning("could not record %s: %s", what, error)
            yield None
            return

        try:
            with connection:
                yield connection
        except sqlite3.Error as error:
            log.warning("could not record %s: %s", what, error)
        finally:
            connection.close()

    # -- writing ---------------------------------------------------------

    def session_started(
        self, *, session_id: str, profile: str, level: str, origin: str, at: float
    ) -> None:
        with self._writing("a session") as connection:
            if connection is None:
                return
            connection.execute(
                "INSERT OR REPLACE INTO sessions "
                "(id, profile, level, origin, started_at) VALUES (?, ?, ?, ?, ?)",
                (session_id, profile, level, origin, at),
            )

    def session_ended(
        self, *, session_id: str, started_at: float, ended_at: float, reason: str
    ) -> None:
        """Close the session and write its focus into the days it covered."""
        completed = 1 if reason == "completed" else 0
        with self._writing("the end of a session") as connection:
            if connection is None:
                return
            connection.execute(
                "UPDATE sessions SET ended_at = ?, reason = ?, completed = ? WHERE id = ?",
                (ended_at, reason, completed, session_id),
            )
            connection.execute("DELETE FROM focus WHERE session_id = ?", (session_id,))
            connection.executemany(
                "INSERT INTO focus (day, session_id, seconds) VALUES (?, ?, ?)",
                [(day, session_id, seconds) for day, seconds in split_by_day(started_at, ended_at)],
            )

    def attempt(self, *, session_id: str | None, kind: str, target: str, at: float) -> None:
        """One refused domain or application (SPEC 13).

        The only place a visited domain is written down.
        """
        with self._writing("a blocked attempt") as connection:
            if connection is None:
                return
            connection.execute(
                "INSERT INTO attempts (at, session_id, kind, target) VALUES (?, ?, ?, ?)",
                (at, session_id, kind, target),
            )

    def break_outcome(self, *, session_id: str | None, outcome: str, at: float) -> None:
        with self._writing("a break") as connection:
            if connection is None:
                return
            connection.execute(
                "INSERT INTO breaks (at, session_id, outcome) VALUES (?, ?, ?)",
                (at, session_id, outcome),
            )

    def rupture(self, *, session_id: str | None, kind: str, at: float) -> None:
        with self._writing("a rupture") as connection:
            if connection is None:
                return
            connection.execute(
                "INSERT INTO ruptures (at, session_id, kind) VALUES (?, ?, ?)",
                (at, session_id, kind),
            )

    # -- keeping it small ------------------------------------------------

    def prune(self, *, now: float) -> int:
        """Forget anything older than the retention window (SPEC 13)."""
        cutoff = now - self.retention_days * 86400
        day_cutoff = datetime.fromtimestamp(cutoff).date().isoformat()
        removed = 0
        with self._writing("the retention sweep") as connection:
            if connection is None:
                return 0
            for table in ("attempts", "breaks", "ruptures"):
                removed += connection.execute(
                    f"DELETE FROM {table} WHERE at < ?",  # noqa: S608 - a fixed list
                    (cutoff,),
                ).rowcount
            removed += connection.execute("DELETE FROM focus WHERE day < ?", (day_cutoff,)).rowcount
            removed += connection.execute(
                "DELETE FROM sessions WHERE ended_at IS NOT NULL AND ended_at < ?", (cutoff,)
            ).rowcount
        return removed

    def delete_everything(self) -> None:
        """The one action that deletes all statistics (SPEC 13).

        Emptied rather than unlinked, so that the next write does not have to
        wonder whether the file exists and what its permissions should be.
        """
        with self._writing("the deletion") as connection:
            if connection is None:
                return
            for table in ("sessions", "focus", "attempts", "breaks", "ruptures"):
                connection.execute(f"DELETE FROM {table}")  # noqa: S608 - a fixed list
        log.info("all statistics were deleted at the user's request")

    # -- reading ---------------------------------------------------------

    def rows(self, query: str, parameters: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        try:
            with self._connect() as connection:
                return list(connection.execute(query, parameters))
        except sqlite3.Error as error:
            log.warning("could not read the statistics: %s", error)
            return []


def range_bounds(view: str, today: date) -> tuple[date, date]:
    """The first and last day of a view, inclusive (SPEC 13, 14).

    The mockup draws the week as Monday to Sunday with a bar for each day, so
    the week is the calendar week rather than the last seven days, and the
    month is the calendar month.
    """
    match view:
        case "day":
            return today, today
        case "week":
            monday = today - timedelta(days=today.weekday())
            return monday, monday + timedelta(days=6)
        case "month":
            first = today.replace(day=1)
            next_month = (first + timedelta(days=32)).replace(day=1)
            return first, next_month - timedelta(days=1)
    raise ValueError(f"unknown range {view!r}")
