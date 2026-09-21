"""The indicator's menu, as ``com.canonical.dbusmenu`` describes it (ADR 3).

ADR 3 chose to speak ``StatusNotifierItem`` directly rather than through
``libayatana-appindicator``, which is a GTK3 library and cannot share a
process with Anchor's GTK4 interface. The cost it recorded was this: the
library hands you a menu for free, and implementing the interface yourself
means exporting one.

The protocol is small but particular. A menu is a tree of numbered items,
where 0 is the root and everything else is a child; the panel asks for the
layout, gets a revision number with it, and re-asks when told the revision
changed. Properties are a dictionary per item, and the panel only asks for
the ones it draws.

Everything here is data. The D-Bus plumbing is next door in
:mod:`anchor.agent.desktop`, and what the menu *says* was decided in
:mod:`anchor.agent.indicator` — this only arranges it.
"""

from __future__ import annotations

from typing import Any, Final

from anchor.agent.indicator import MenuItem

#: The root item, which every menu has and nobody sees.
ROOT_ID: Final = 0

#: The dbusmenu revision this implements. 3 is what every panel speaks.
VERSION: Final = 3


def with_separator(items: tuple[MenuItem, ...]) -> tuple[MenuItem, ...]:
    """The menu, with a line between what it says and what it does.

    SPEC 14.1 lists four things to read and two to click. A separator between
    them is how every other menu on the desktop says "the rest of this is
    buttons", and without it the two actions look like more information.
    """
    for index, item in enumerate(items):
        if item.action:
            if index == 0:
                break
            return (*items[:index], MenuItem(label="", action="separator"), *items[index:])
    return items


def properties_of(item: MenuItem) -> dict[str, Any]:
    """One item's properties, as the panel reads them."""
    if item.action == "separator":
        return {"type": "separator", "visible": True}
    return {
        "label": _escaped(item.label),
        "enabled": bool(item.action) and item.enabled,
        "visible": True,
    }


def _escaped(label: str) -> str:
    """``_`` means "the next letter is the shortcut" in a menu label.

    Anchor's labels are sentences, not commands, and none of them wants a
    mnemonic. Doubling the underscore is how a literal one is written, and
    without it a profile called ``deep_work`` would lose a character and gain
    an accelerator nobody asked for.
    """
    return label.replace("_", "__")


def layout_for(items: tuple[MenuItem, ...]) -> tuple[int, dict[str, Any], list[Any]]:
    """The whole tree: the root, its properties, and its children.

    Shaped for D-Bus's ``(ia{sv}av)``, where the last part is a list of
    children each of the same shape.
    """
    children: list[tuple[int, dict[str, Any], list[Any]]] = [
        (index, properties_of(item), [])
        for index, item in enumerate(with_separator(items), start=1)
    ]
    return ROOT_ID, {"children-display": "submenu"}, children


def group_properties(items: tuple[MenuItem, ...], ids: list[int]) -> list[Any]:
    """The properties of the items the panel asked about, and no others."""
    numbered = dict(enumerate(with_separator(items), start=1))
    wanted = sorted(set(ids) & set(numbered)) if ids else sorted(numbered)
    return [(index, properties_of(numbered[index])) for index in wanted]


def action_at(items: tuple[MenuItem, ...], item_id: int) -> str:
    """What clicking item ``item_id`` should do, or nothing.

    A click on a line that is only information is not an error and not an
    action: menus send events for anything the pointer lands on.
    """
    numbered = dict(enumerate(with_separator(items), start=1))
    item = numbered.get(item_id)
    if item is None or item.action == "separator" or not item.enabled:
        return ""
    return item.action
