"""Session time that survives suspend, reboot and a wound-back clock (SPEC 6.2).

A session end is stored as an absolute UTC timestamp, but the wall clock is
under the user's control, so it cannot be the only source of truth. Within one
boot the authority is ``CLOCK_BOOTTIME``, which counts suspended time and
cannot be set by anyone. Anchor compares the two: when they disagree by more
than :data:`CLOCK_TOLERANCE_SECONDS` the wall clock was moved, which is a
rupture, and the boot-clock reading decides how much of the session is left.

Across a reboot ``CLOCK_BOOTTIME`` restarts from zero, so the absolute end
timestamp is all that remains. SPEC 16 records that as a known residual risk:
a reboot combined with a clock change is harder to detect.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final, Protocol, Self, runtime_checkable

#: How far the wall clock may drift from the boot clock before Anchor calls it
#: tampering. Time synchronisation corrects by small amounts, and a resume can
#: land a moment off, so the threshold sits above routine noise and well below
#: any change a person would make to buy themselves time.
CLOCK_TOLERANCE_SECONDS: Final = 60.0

_BOOT_ID_PATH: Final = Path("/proc/sys/kernel/random/boot_id")


@runtime_checkable
class Clock(Protocol):
    """The three readings Anchor needs to reason about time."""

    def wall(self) -> float:
        """Seconds since the Unix epoch, UTC. Settable by the user."""

    def boottime(self) -> float:
        """Seconds since boot, including suspended time. Not settable."""

    def boot_id(self) -> str:
        """Identifier of the current boot, so reboots can be recognised."""


class SystemClock:
    """The real clock."""

    __slots__ = ()

    def wall(self) -> float:
        return time.time()

    def boottime(self) -> float:
        return time.clock_gettime(time.CLOCK_BOOTTIME)

    def boot_id(self) -> str:
        try:
            return _BOOT_ID_PATH.read_text(encoding="ascii").strip()
        except OSError:
            # Without a boot identifier every reconciliation is treated as a
            # reboot, which falls back to the absolute timestamp. That is the
            # safe direction: it never silently extends a session.
            return ""


class FakeClock:
    """A controllable clock, for tests and for the engine's own simulations."""

    __slots__ = ("_boot", "_boottime", "_wall")

    def __init__(self, *, wall_time: float, boottime: float, boot: str) -> None:
        self._wall = wall_time
        self._boottime = boottime
        self._boot = boot

    def wall(self) -> float:
        return self._wall

    def boottime(self) -> float:
        return self._boottime

    def boot_id(self) -> str:
        return self._boot

    def advance(self, seconds: float) -> None:
        """Let ``seconds`` pass normally, both clocks moving together."""
        self._wall += seconds
        self._boottime += seconds

    def suspend(self, seconds: float) -> None:
        """Sleep for ``seconds``. Both clocks keep counting (SPEC 6.2)."""
        self.advance(seconds)

    def warp_wall_clock(self, seconds: float) -> None:
        """Move the wall clock only, as ``timedatectl set-time`` would."""
        self._wall += seconds

    def reboot(self, *, new_boot: str, wall_elapsed: float, boottime_at_start: float) -> None:
        """Restart the machine: a new boot identifier and a reset boot clock."""
        self._wall += wall_elapsed
        self._boottime = boottime_at_start
        self._boot = new_boot


@dataclass(frozen=True, slots=True)
class TimeAnchor:
    """Everything needed to say how much of a session is left."""

    started_at: float
    """Wall-clock instant the session started, UTC."""

    ends_at: float
    """Wall-clock instant the session ends, UTC (SPEC 6.2)."""

    boot_id: str
    """Boot the boot-clock readings below belong to."""

    started_at_boottime: float
    """``CLOCK_BOOTTIME`` when the session started."""

    duration_seconds: float
    """Total length, including every extension granted so far."""

    @classmethod
    def start(cls, clock: Clock, *, duration_seconds: float) -> Self:
        if duration_seconds <= 0:
            raise ValueError("a session needs a positive duration")
        now = clock.wall()
        return cls(
            started_at=now,
            ends_at=now + duration_seconds,
            boot_id=clock.boot_id(),
            started_at_boottime=clock.boottime(),
            duration_seconds=float(duration_seconds),
        )

    def extended_by(self, seconds: float) -> Self:
        """Return an anchor lengthened by ``seconds`` (SPEC 7.3)."""
        if seconds <= 0:
            raise ValueError("an extension must be positive")
        return replace(
            self,
            ends_at=self.ends_at + seconds,
            duration_seconds=self.duration_seconds + seconds,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "ends_at": self.ends_at,
            "boot_id": self.boot_id,
            "started_at_boottime": self.started_at_boottime,
            "duration_seconds": self.duration_seconds,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Self:
        return cls(
            started_at=float(raw["started_at"]),
            ends_at=float(raw["ends_at"]),
            boot_id=str(raw["boot_id"]),
            started_at_boottime=float(raw["started_at_boottime"]),
            duration_seconds=float(raw["duration_seconds"]),
        )


@dataclass(frozen=True, slots=True)
class Reconciliation:
    """What the engine learned by comparing an anchor against the clock."""

    anchor: TimeAnchor
    """The anchor to store from now on, re-pegged if the boot changed."""

    remaining_seconds: float
    """Time left, never negative."""

    expired: bool
    """Whether the session is over."""

    tampered: bool
    """Whether the wall clock disagreed with the boot clock (a rupture)."""

    rebooted: bool
    """Whether this reading crossed a reboot."""

    wall_drift_seconds: float = 0.0
    """How far the wall clock moved, for the rupture record."""


def reconcile(anchor: TimeAnchor, clock: Clock) -> Reconciliation:
    """Work out the state of a session at the current instant.

    Called on every tick, and on boot and resume, where the specification
    requires expired sessions to end and active ones to resume with the time
    they have left.
    """
    now_wall = clock.wall()
    now_boottime = clock.boottime()
    current_boot = clock.boot_id()

    same_boot = bool(anchor.boot_id) and current_boot == anchor.boot_id

    if same_boot:
        elapsed = now_boottime - anchor.started_at_boottime
        expected_wall = anchor.started_at + elapsed
        drift = now_wall - expected_wall
        tampered = abs(drift) > CLOCK_TOLERANCE_SECONDS
        remaining = anchor.duration_seconds - elapsed

        # The boot clock decides, so both wall-clock readings are re-pegged to
        # the clock the machine now shows. Re-pegging the end alone would leave
        # the origin stale, and every later tick would report the same jump
        # again as a fresh rupture.
        updated = (
            replace(anchor, started_at=now_wall - elapsed, ends_at=now_wall + remaining)
            if tampered
            else anchor
        )

        return Reconciliation(
            anchor=updated,
            remaining_seconds=max(0.0, remaining),
            expired=remaining <= 0,
            tampered=tampered,
            rebooted=False,
            wall_drift_seconds=drift if tampered else 0.0,
        )

    # A different boot: the boot clock restarted, so fall back to the absolute
    # end and re-peg the boot readings to the current boot.
    remaining = anchor.ends_at - now_wall
    rebased = replace(
        anchor,
        boot_id=current_boot,
        started_at_boottime=now_boottime - (anchor.duration_seconds - remaining),
    )
    return Reconciliation(
        anchor=rebased,
        remaining_seconds=max(0.0, remaining),
        expired=remaining <= 0,
        tampered=False,
        rebooted=True,
    )
