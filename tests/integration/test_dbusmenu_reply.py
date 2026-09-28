"""The one piece of the D-Bus layer that can be checked without a bus (ADR 3).

ADR 3 accepted that speaking StatusNotifierItem directly means exporting the
menu ourselves, and that the D-Bus plumbing would be the half without tests.
This is the part of it that did not have to be: building the GetLayout reply
is pure data, and getting it wrong crashed the agent on the owner's desktop
about once a second -- which is how often the panel asks for the layout --
while the indicator went on working, so Ubuntu reported a crashed
application for a menu that was never drawn.

Skipped where PyGObject is missing, which includes continuous integration.
A test that runs on the machines that have a desktop is worth more here than
no test at all.
"""

from __future__ import annotations

import pytest

gi = pytest.importorskip("gi", reason="PyGObject is not installed here")
from gi.repository import GLib  # noqa: E402

from anchor.agent.desktop import layout_reply  # noqa: E402
from anchor.agent.indicator import MenuItem  # noqa: E402


@pytest.fixture
def items() -> tuple[MenuItem, ...]:
    """What SPEC 14.1 puts in the menu: things to read, then things to click.

    Raw: layout_for puts the separator in, and doing it here as well gives
    the menu two of them.
    """
    return (
        MenuItem(label="Study - 0:10:00"),
        MenuItem(label="Next break in 45 min"),
        MenuItem(label="Open Anchor", action="open"),
        MenuItem(label="Extend session", action="extend"),
    )


class TestTheLayoutReply:
    def test_it_has_the_signature_the_panel_expects(self, items: tuple[MenuItem, ...]) -> None:
        reply = layout_reply(GLib, 7, items)

        assert reply.get_type_string() == "(u(ia{sv}av))"

    def test_the_revision_comes_back(self, items: tuple[MenuItem, ...]) -> None:
        revision, _layout = layout_reply(GLib, 7, items).unpack()

        assert revision == 7

    def test_every_item_survives_the_round_trip(self, items: tuple[MenuItem, ...]) -> None:
        """Unpacked, because a reply the panel cannot read is not a reply."""
        _revision, layout = layout_reply(GLib, 1, items).unpack()
        _root, _properties, children = layout

        # One more than the items: layout_for adds the separator between
        # what the menu says and what it does.
        assert len(children) == len(items) + 1
        labels = [child[1].get("label") for child in children]
        assert "Open Anchor" in labels
        assert "Extend session" in labels

    def test_the_old_way_is_what_crashed(self, items: tuple[MenuItem, ...]) -> None:
        """Pinned, so nobody reaches for the obvious spelling again.

        Nesting a built Variant inside a format string looks right and is
        not: PyGObject walks into it and unpacks it back to plain Python.
        """
        reply = layout_reply(GLib, 1, items)
        _revision, layout = reply.unpack()

        with pytest.raises(TypeError, match="Expected GLib.Variant"):
            GLib.Variant("(u(ia{sv}av))", (1, GLib.Variant("(ia{sv}av)", layout)))
