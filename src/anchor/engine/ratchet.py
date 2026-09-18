"""The ratchet: during a session, only stricter changes apply (SPEC 7.4, P3).

Accepted while a session runs: adding domains, adding apps, extending. Refused:
removing anything, shortening, lowering the level, changing the valve, and
turning an allowlist back into a blocklist.

"Stricter" depends on the mode. In blocklist mode the listed domains are the
blocked ones, so adding to the list blocks more. In allowlist mode the listed
domains are the only reachable ones, so the sense is inverted: adding an entry
opens a site up, and removing one closes it. Handling both directions with the
same rule is what stops an allowlist session from being unwound one entry at a
time.
"""

from __future__ import annotations

from collections.abc import Set as AbstractSet

from anchor.engine.profiles import Profile
from anchor.protocol.errors import RatchetViolationError
from anchor.protocol.types import Level, Valve, WebMode


def _quote(names: AbstractSet[str]) -> str:
    return ", ".join(sorted(names))


def check_profile_change(before: Profile, after: Profile) -> None:
    """Raise :class:`RatchetViolationError` if ``after`` is looser than ``before``.

    Called before any profile edit is applied to a running session.
    """
    if before.web_mode is WebMode.ALLOWLIST and after.web_mode is WebMode.BLOCKLIST:
        raise RatchetViolationError(
            "cannot turn the allowlist back into a blocklist during a session: "
            "an allowlist blocks everything it does not name, so this would "
            "unblock the rest of the web"
        )

    if after.web_mode is WebMode.ALLOWLIST and before.web_mode is WebMode.ALLOWLIST:
        # Inverted sense: the list names what stays reachable.
        opened = after.domains - before.domains
        if opened:
            raise RatchetViolationError(
                f"cannot add {_quote(opened)} to the allowlist during a session: "
                "it would unblock those sites"
            )
    else:
        removed = before.domains - after.domains
        if removed:
            raise RatchetViolationError(f"cannot unblock {_quote(removed)} during a session")

    removed_categories = before.categories - after.categories
    if removed_categories:
        raise RatchetViolationError(
            f"cannot unblock the {_quote(removed_categories)} category during a session"
        )

    removed_apps = before.apps - after.apps
    if removed_apps:
        raise RatchetViolationError(
            f"cannot unblock the app {_quote(removed_apps)} during a session"
        )

    if before.breaks.allow_sites_during_breaks is False and after.breaks.allow_sites_during_breaks:
        raise RatchetViolationError(
            "cannot start allowing blocked sites during breaks while a session runs"
        )


def check_session_change(
    level_before: Level,
    level_after: Level,
    *,
    valve_before: Valve | None,
    valve_after: Valve | None,
) -> None:
    """Raise if the level would drop or the valve would change (SPEC 7.4, 7.5)."""
    if not level_after.is_at_least(level_before):
        raise RatchetViolationError(
            f"cannot lower the level from {level_before} to {level_after} during a session"
        )

    if level_after is level_before and valve_after != valve_before:
        raise RatchetViolationError(
            "the emergency valve is chosen when the session starts and cannot be changed"
        )


def check_duration_change(current_seconds: float, proposed_seconds: float) -> None:
    """Raise if a session would be shortened (SPEC 7.4)."""
    if proposed_seconds < current_seconds:
        raise RatchetViolationError("a session can be extended but never shortened")
