"""The Profiles screen (SPEC 12, mockup 4).

A profile is what a session enforces: a web mode, the categories it turns on,
its own domains, the applications it closes, and how breaks behave. The mockup
puts all of it on one screen with a Save button.

The part that needs care is what the screen does while a session is running.
SPEC 7.4 allows only stricter changes then, and the engine refuses the rest —
but a button that can be pressed and then produces an error is worse than one
that is visibly disabled with the reason underneath. So every lock here mirrors
:mod:`anchor.engine.ratchet` exactly, including the two places where the sense
inverts:

* In **blocklist** mode the list names what is blocked, so *removing* is the
  loosening and is barred.
* In **allowlist** mode the list names what is reachable, so *adding* is the
  loosening and is barred instead.

Nothing here is locked that the engine would allow. In particular the break
pattern, its type and its hardness stay editable mid-session, because the
ratchet is about what is blocked and a shorter work period is not a way out.
Only "allow blocked sites during breaks" is held, and only in the direction
that opens something.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from anchor.gui.i18n import _
from anchor.protocol.types import BreakHardness, BreakType, WebMode

#: The patterns the mockup offers, plus "custom" for anything else.
PATTERNS: Final = ((25, 5), (50, 10), (90, 20))

CUSTOM: Final = "custom"


@dataclass(frozen=True, slots=True)
class Choice:
    """One option of a group: what it is, and what choosing it means."""

    key: str
    title: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Toggle:
    """A tick box, and why it may not be untickable."""

    key: str
    title: str
    detail: str = ""
    on: bool = False
    locked: bool = False
    reason: str = ""


@dataclass(frozen=True, slots=True)
class Entry:
    """One domain in the profile's own list."""

    value: str
    removable: bool = True
    reason: str = ""


@dataclass(frozen=True, slots=True)
class AppRow:
    """One installed application the user may tick (SPEC 9)."""

    key: str
    name: str
    icon: str = ""
    detail: str = ""
    on: bool = False
    locked: bool = False
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ProfileScreen:
    """The whole screen (mockup 4)."""

    name: str

    mode: str = str(WebMode.BLOCKLIST)
    mode_locked: bool = False
    mode_reason: str = ""
    allowlist_warning: str = ""

    categories: tuple[Toggle, ...] = ()
    domains: tuple[Entry, ...] = ()
    can_add_domains: bool = True
    add_reason: str = ""

    apps: tuple[AppRow, ...] = ()

    patterns: tuple[Choice, ...] = ()
    pattern: str = ""
    types: tuple[Choice, ...] = ()
    type: str = ""
    hardnesses: tuple[Choice, ...] = ()
    hardness: str = ""
    allow_sites: Toggle = Toggle(key="allow_sites", title="")

    in_use: bool = False
    notice: str = ""


def profile_view(
    *,
    profile: dict[str, Any],
    categories: list[dict[str, Any]],
    apps: list[dict[str, Any]],
    in_use: bool = False,
) -> ProfileScreen:
    """The screen for one profile, with the ratchet's locks already applied."""
    mode = str(profile.get("web_mode", WebMode.BLOCKLIST))
    allowlist = mode == str(WebMode.ALLOWLIST)
    breaks = profile.get("breaks") or {}
    chosen_categories = set(profile.get("categories") or ())
    chosen_apps = set(profile.get("apps") or ())

    # In an allowlist it is adding that opens a site up, so that is the
    # direction the ratchet holds (SPEC 7.4).
    removing_barred = in_use and not allowlist
    adding_barred = in_use and allowlist

    return ProfileScreen(
        name=str(profile.get("name", "")),
        mode=mode,
        mode_locked=in_use and allowlist,
        mode_reason=(
            _(
                "An allowlist blocks everything it does not name, so turning it "
                "back into a blocklist would unblock the rest of the web."
            )
            if in_use and allowlist
            else ""
        ),
        allowlist_warning=_(
            "An allowlist blocks all of the web except what you add. Many sites "
            "load parts of themselves from other domains and will need entries "
            "of their own."
        ),
        categories=_categories(categories, chosen_categories, locked=removing_barred),
        domains=_domains(profile, removable=not removing_barred),
        can_add_domains=not adding_barred,
        add_reason=(
            _("Adding to an allowlist would unblock a site, which a session refuses.")
            if adding_barred
            else ""
        ),
        apps=_apps(apps, chosen_apps, categories, chosen_categories, locked=removing_barred),
        patterns=_patterns(),
        pattern=_pattern_of(breaks),
        types=_types(),
        type=str(breaks.get("type", BreakType.OVERLAY)),
        hardnesses=_hardnesses(),
        hardness=str(breaks.get("hardness", BreakHardness.MODERATE)),
        allow_sites=_allow_sites(breaks, in_use=in_use),
        in_use=in_use,
        notice=(
            _("A session is using this profile, so it can only be made stricter.") if in_use else ""
        ),
    )


# -- the lists -----------------------------------------------------------


def _categories(
    categories: list[dict[str, Any]], chosen: set[str], *, locked: bool
) -> tuple[Toggle, ...]:
    rows: list[Toggle] = []
    for entry in sorted(categories, key=lambda one: str(one.get("name", ""))):
        key = str(entry.get("id", ""))
        on = key in chosen
        rows.append(
            Toggle(
                key=key,
                title=str(entry.get("name", key)),
                detail=_covers(entry),
                on=on,
                locked=locked and on,
                reason=(
                    _("A session cannot stop blocking a category it is blocking.")
                    if locked and on
                    else ""
                ),
            )
        )
    return tuple(rows)


def _covers(entry: dict[str, Any]) -> str:
    """``4 sites · 1 app``, so a tick box is not a guess."""
    sites = len(entry.get("domains") or ())
    apps = len(entry.get("apps") or ())
    said = _("1 site") if sites == 1 else _("{n} sites").format(n=sites)
    if apps:
        said += " · " + (_("1 app") if apps == 1 else _("{n} apps").format(n=apps))
    return said


def _domains(profile: dict[str, Any], *, removable: bool) -> tuple[Entry, ...]:
    reason = "" if removable else _("A domain cannot be unblocked during a session.")
    return tuple(
        Entry(value=str(domain), removable=removable, reason=reason)
        for domain in sorted(profile.get("domains") or ())
    )


def _apps(
    apps: list[dict[str, Any]],
    chosen: set[str],
    categories: list[dict[str, Any]],
    enabled_categories: set[str],
    *,
    locked: bool,
) -> tuple[AppRow, ...]:
    by_app: dict[str, list[str]] = {}
    for entry in categories:
        if str(entry.get("id", "")) not in enabled_categories:
            continue
        for app in entry.get("apps") or ():
            by_app.setdefault(str(app), []).append(str(entry.get("name", "")))

    rows: list[AppRow] = []
    for app in sorted(apps, key=lambda one: str(one.get("name", ""))):
        key = str(app.get("id", ""))
        on = key in chosen
        bundled = by_app.get(key, [])
        rows.append(
            AppRow(
                key=key,
                name=str(app.get("name", key)),
                icon=str(app.get("icon", "")),
                detail=(
                    _("Already blocked by {category}").format(category=", ".join(bundled))
                    if bundled
                    else ""
                ),
                on=on,
                locked=locked and on,
                reason=(
                    _("A session cannot stop blocking an application it is blocking.")
                    if locked and on
                    else ""
                ),
            )
        )
    return tuple(rows)


# -- the breaks ----------------------------------------------------------


def _patterns() -> tuple[Choice, ...]:
    rows = [Choice(key=f"{work}/{rest}", title=f"{work}/{rest}") for work, rest in PATTERNS]
    rows.append(Choice(key=CUSTOM, title=_("Custom")))
    return tuple(rows)


def _pattern_of(breaks: dict[str, Any]) -> str:
    work = int(breaks.get("work_minutes", 50))
    rest = int(breaks.get("break_minutes", 10))
    if (work, rest) in PATTERNS:
        return f"{work}/{rest}"
    return CUSTOM


def _types() -> tuple[Choice, ...]:
    return (
        Choice(
            key=str(BreakType.NOTIFICATION),
            title=_("Notification"),
            detail=_("A message, and nothing in your way."),
        ),
        Choice(
            key=str(BreakType.OVERLAY),
            title=_("Fullscreen"),
            detail=_("A countdown on every monitor."),
        ),
    )


def _hardnesses() -> tuple[Choice, ...]:
    """SPEC 10's table, in words rather than ticks and crosses."""
    return (
        Choice(
            key=str(BreakHardness.FLEXIBLE),
            title=_("Flexible"),
            detail=_("You can skip it, or postpone it as often as you like."),
        ),
        Choice(
            key=str(BreakHardness.MODERATE),
            title=_("Moderate"),
            detail=_("No skipping. It can be postponed once, by five minutes."),
        ),
        Choice(
            key=str(BreakHardness.MANDATORY),
            title=_("Mandatory"),
            detail=_("Neither skipping nor postponing."),
        ),
    )


def _allow_sites(breaks: dict[str, Any], *, in_use: bool) -> Toggle:
    on = bool(breaks.get("allow_sites_during_breaks", False))
    # Only the direction that opens something is held (SPEC 7.4).
    locked = in_use and not on
    return Toggle(
        key="allow_sites_during_breaks",
        title=_("Blocked sites reachable during breaks"),
        detail=_("Applications stay closed either way."),
        on=on,
        locked=locked,
        reason=(_("This cannot be turned on while a session is running.") if locked else ""),
    )


# -- saving --------------------------------------------------------------


def edit_request(
    before: dict[str, Any], after: dict[str, Any]
) -> tuple[str, dict[str, Any]] | None:
    """The request Save sends, or ``None`` when nothing changed.

    Additions and removals rather than a whole profile, because that is what
    ``profile.edit`` takes and what the ratchet judges.
    """
    payload: dict[str, Any] = {"name": str(before.get("name", ""))}

    for field, singular in (("domains", "domain"), ("apps", "app"), ("categories", "category")):
        was = set(before.get(field) or ())
        now = set(after.get(field) or ())
        del singular
        if added := sorted(now - was):
            payload[f"add_{field}"] = added
        if removed := sorted(was - now):
            payload[f"remove_{field}"] = removed

    if before.get("web_mode") != after.get("web_mode"):
        payload["web_mode"] = str(after.get("web_mode"))

    if before.get("block_vpn_and_tor") != after.get("block_vpn_and_tor"):
        payload["block_vpn_and_tor"] = bool(after.get("block_vpn_and_tor"))

    if changes := _break_changes(before.get("breaks") or {}, after.get("breaks") or {}):
        payload["breaks"] = changes

    return ("profile.edit", payload) if len(payload) > 1 else None


def _break_changes(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Only the break keys that moved.

    Dropping the long break is the one change that cannot be said by leaving a
    key out, because absent already means "unchanged"; a zero says it.
    """
    changes: dict[str, Any] = {}
    for key in (
        "work_minutes",
        "break_minutes",
        "type",
        "hardness",
        "allow_sites_during_breaks",
        "warning_seconds",
        "long_break_minutes",
    ):
        if key in after and after.get(key) != before.get(key) and after.get(key) is not None:
            changes[key] = after[key]

    if before.get("long_break_every") != after.get("long_break_every"):
        changes["long_break_every"] = int(after.get("long_break_every") or 0)
    return changes
