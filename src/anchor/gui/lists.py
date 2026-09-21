"""The Lists screen: the categories, and what is in them (SPEC 12, 15).

The navigation bar in the mockups has six entries and the mockups draw four of
them. This is one of the two they do not draw, so it is built in the same
idiom as the ones they do rather than invented in a different one.

What belongs here is settled by the rest of the specification. SPEC 12 says
categories are "stored as data files, editable by the user, updated with the
package"; SPEC 15 gives them ``anchor category list|show|edit``; SPEC 9 says a
category bundles an application with its domains. A screen that shows each
category, what it covers, whether you are looking at the shipped list or your
own copy, and lets you change it, is all four of those in one place.

Profiles tick categories on and off; this screen is about what a tick *means*.
The two are deliberately apart: a profile is a choice for one kind of session,
and a category is a fact about the world that every profile shares.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from anchor.gui.i18n import _


@dataclass(frozen=True, slots=True)
class Item:
    """One entry of a category, and whether it may be taken out."""

    value: str
    removable: bool = True
    reason: str = ""


@dataclass(frozen=True, slots=True)
class CategoryCard:
    """One category, as the screen shows it."""

    id: str
    name: str
    summary: str
    domains: tuple[Item, ...] = ()
    apps: tuple[Item, ...] = ()
    custom: bool = False
    origin: str = ""
    blocked_now: bool = False
    can_remove: bool = True
    locked_reason: str = ""


@dataclass(frozen=True, slots=True)
class ListsView:
    """The whole screen."""

    categories: tuple[CategoryCard, ...] = ()
    note: str = ""
    empty: str = ""


def lists_view(
    categories: list[dict[str, Any]],
    *,
    blocked_now: set[str] | None = None,
) -> ListsView:
    """Every category this machine knows.

    ``blocked_now`` is the set a running session is enforcing, which the
    caller works out from the session's profile. Entries in those categories
    cannot be taken out until it ends (SPEC 7.4), and the screen says so
    rather than letting the click fail.
    """
    enforcing = blocked_now or set()

    cards: list[CategoryCard] = []
    for entry in sorted(categories, key=lambda one: str(one.get("name", ""))):
        identifier = str(entry.get("id", ""))
        held = identifier in enforcing
        reason = (
            _("A session is blocking this category, so nothing can be taken out of it.")
            if held
            else ""
        )
        cards.append(
            CategoryCard(
                id=identifier,
                name=str(entry.get("name", identifier)),
                summary=_summary(entry),
                domains=_items(entry.get("domains"), removable=not held, reason=reason),
                apps=_items(entry.get("apps"), removable=not held, reason=reason),
                custom=bool(entry.get("custom")),
                origin=(
                    _("Your copy. Package updates will not change it.")
                    if entry.get("custom")
                    else _("Shipped with Anchor, and updated with it.")
                ),
                blocked_now=held,
                can_remove=not held,
                locked_reason=reason,
            )
        )

    return ListsView(
        categories=tuple(cards),
        note=_(
            "Editing a category saves your own copy of it. The list Anchor ships "
            "keeps being updated, and your copy is what gets used."
        ),
        empty=_("No categories are installed on this machine."),
    )


def _summary(entry: dict[str, Any]) -> str:
    domains = len(entry.get("domains") or ())
    apps = len(entry.get("apps") or ())
    said = _("1 site") if domains == 1 else _("{n} sites").format(n=domains)
    if apps:
        said += " · " + (_("1 app") if apps == 1 else _("{n} apps").format(n=apps))
    return said


def _items(values: Any, *, removable: bool, reason: str) -> tuple[Item, ...]:
    return tuple(
        Item(value=str(value), removable=removable, reason=reason) for value in sorted(values or ())
    )


def edit_request(
    identifier: str,
    *,
    add_domains: list[str] | None = None,
    remove_domains: list[str] | None = None,
    add_apps: list[str] | None = None,
    remove_apps: list[str] | None = None,
    rename: str | None = None,
) -> tuple[str, dict[str, Any]] | None:
    """The request an edit sends, or ``None`` when nothing was asked for."""
    payload: dict[str, Any] = {"id": identifier}
    for key, values in (
        ("add_domains", add_domains),
        ("remove_domains", remove_domains),
        ("add_apps", add_apps),
        ("remove_apps", remove_apps),
    ):
        if values:
            payload[key] = sorted(set(values))
    if rename:
        payload["name"] = rename
    return ("category.edit", payload) if len(payload) > 1 else None
