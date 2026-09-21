"""The Settings screen (SPEC 7.2, 13, 14).

Four things a person chooses: how long a Firm session makes them wait, how
long the random phrase is, how long statistics are kept, and which language
the interface speaks. The engine owns all four and describes them itself, so
this screen is mostly presentation — which is the point, because
``anchor config`` and this screen must not disagree about what is allowed.

Two decisions.

**Anchor's note to itself is not a setting.** ``onboarding_done`` is stored
next to the others and does not belong on a page of choices; showing it would
invite someone to flip it and wonder what they broke.

**A locked setting stays visible.** SPEC 7.2 freezes the two exit knobs for
the length of a session. Hiding them would read as "Anchor does not do that";
showing them greyed out with the reason underneath reads as what it is, which
is the whole point of a tool built out of friction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from anchor.cli.durations import format_duration
from anchor.gui.i18n import _

#: The settings this screen shows, in the order it shows them. Anything the
#: engine knows about and this does not is Anchor's own bookkeeping.
SHOWN: Final = ("firm_wait_seconds", "phrase_length", "retention_days", "language")


@dataclass(frozen=True, slots=True)
class Choice:
    key: str
    title: str


@dataclass(frozen=True, slots=True)
class Row:
    """One setting, as a line on the page."""

    key: str
    title: str
    value: str
    explain: str
    kind: str = "number"
    """``duration``, ``number`` or ``choice``: what control to draw."""

    choices: tuple[Choice, ...] = ()
    raw: Any = None
    is_default: bool = True
    locked: bool = False
    reason: str = ""


@dataclass(frozen=True, slots=True)
class SettingsView:
    rows: tuple[Row, ...] = ()
    notice: str = ""
    danger_title: str = ""
    delete_label: str = ""
    delete_warning: str = ""


def settings_view(*, settings: list[dict[str, Any]], in_session: bool = False) -> SettingsView:
    """The page, from what ``config.get`` answered."""
    described = {str(entry.get("key", "")): entry for entry in settings}

    rows: list[Row] = []
    for key in SHOWN:
        entry = described.get(key)
        if entry is None:
            continue
        locked = in_session and not entry.get("during_session", True)
        rows.append(
            Row(
                key=key,
                title=_title(key),
                value=_shown(key, entry.get("value")),
                explain=str(entry.get("explain", "")),
                kind=_kind(key),
                choices=_choices(key),
                raw=entry.get("value"),
                is_default=bool(entry.get("is_default", False)),
                locked=locked,
                reason=(
                    _(
                        "This decides how hard the running session is to leave, "
                        "and that was settled when it started."
                    )
                    if locked
                    else ""
                ),
            )
        )

    return SettingsView(
        rows=tuple(rows),
        notice=(
            _("A session is running, so the two exit settings cannot change.") if in_session else ""
        ),
        danger_title=_("Statistics"),
        delete_label=_("Delete all statistics"),
        delete_warning=_(
            "This deletes everything Anchor has recorded. It cannot be undone: "
            "Anchor keeps no copy, because a private record that survives its "
            "own deletion is not private."
        ),
    )


def set_request(key: str, value: Any) -> tuple[str, dict[str, Any]]:
    """One change, as the engine takes it.

    Values cross as text and the engine judges them, so that this screen and
    ``anchor config set`` cannot disagree about what a setting may hold.
    """
    return "config.set", {"key": key, "value": "" if value is None else str(value)}


# -- how each one reads --------------------------------------------------


def _title(key: str) -> str:
    return {
        "firm_wait_seconds": _("Firm session wait"),
        "phrase_length": _("Random phrase length"),
        "retention_days": _("Keep statistics for"),
        "language": _("Language"),
    }.get(key, key)


def _kind(key: str) -> str:
    if key == "language":
        return "choice"
    return "duration" if key == "firm_wait_seconds" else "number"


def _choices(key: str) -> tuple[Choice, ...]:
    if key != "language":
        return ()
    return (
        Choice(key="", title=_("Follow the desktop")),
        Choice(key="en", title=_("English")),
        Choice(key="es", title=_("Spanish")),
    )


def _shown(key: str, value: Any) -> str:
    if key == "firm_wait_seconds":
        return format_duration(float(value or 0))
    if key == "phrase_length":
        return _("{n} characters").format(n=int(value or 0))
    if key == "retention_days":
        days = int(value or 0)
        return _("1 day") if days == 1 else _("{n} days").format(n=days)
    if key == "language":
        for choice in _choices(key):
            if choice.key == str(value or ""):
                return choice.title
    return str(value)
