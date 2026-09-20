#!/usr/bin/env python3
"""Register a StatusNotifierItem over D-Bus, with no AppIndicator library.

Run by ``s4_indicator.py`` as a separate process. It prints machine-readable
lines rather than returning values, so a crash in the bindings cannot take the
spike down with it.

The point is to find out whether Anchor can show a top-bar indicator using
nothing but the D-Bus interface GNOME's AppIndicator extension already watches.
If it can, the indicator stays inside ``anchor-agent`` as SPEC 5.1 wants, and
Anchor needs no GTK3 library in a GTK4 process.
"""

from __future__ import annotations

import os
import sys

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

WATCHER_NAME = "org.kde.StatusNotifierWatcher"
WATCHER_PATH = "/StatusNotifierWatcher"
ITEM_PATH = "/StatusNotifierItem"
BUS_NAME = f"org.kde.StatusNotifierItem-{os.getpid()}-1"

#: The properties a watcher reads. XAyatanaLabel is the extension's way of
#: showing text beside the icon, which SPEC 14.1 needs for the remaining time.
INTROSPECTION = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconThemePath" type="s" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <property name="XAyatanaLabel" type="s" access="read"/>
    <property name="XAyatanaLabelGuide" type="s" access="read"/>
    <method name="Activate">
      <arg type="i" name="x" direction="in"/>
      <arg type="i" name="y" direction="in"/>
    </method>
    <signal name="NewIcon"/>
    <signal name="NewStatus"><arg type="s" name="status"/></signal>
    <signal name="NewLabel">
      <arg type="s" name="label"/>
      <arg type="s" name="guide"/>
    </signal>
  </interface>
</node>
"""

PROPERTIES = {
    "Category": ("s", "ApplicationStatus"),
    "Id": ("s", "anchor-spike"),
    "Title": ("s", "Anchor"),
    "Status": ("s", "Active"),
    "IconName": ("s", "alarm-symbolic"),
    "IconThemePath": ("s", ""),
    "ItemIsMenu": ("b", False),
    "Menu": ("o", "/MenuBar"),
    "XAyatanaLabel": ("s", "2:14"),
    "XAyatanaLabelGuide": ("s", "0:00"),
}

SECONDS = 6


def say(*parts: object) -> None:
    print(*parts, flush=True)


def _on_method(
    _conn: object,
    _sender: str,
    _path: str,
    _iface: str,
    method: str,
    _params: object,
    invocation: Gio.DBusMethodInvocation,
) -> None:
    say("ACTIVATED", method)
    invocation.return_value(None)


def _on_get(_conn: object, _sender: str, _path: str, _iface: str, name: str) -> GLib.Variant | None:
    entry = PROPERTIES.get(name)
    return GLib.Variant(entry[0], entry[1]) if entry else None


def main() -> int:
    try:
        connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    except GLib.Error as error:
        say("NO_BUS", error.message)
        return 0

    node = Gio.DBusNodeInfo.new_for_xml(INTROSPECTION)
    try:
        connection.register_object(ITEM_PATH, node.interfaces[0], _on_method, _on_get, None)
    except GLib.Error as error:
        say("NO_EXPORT", error.message)
        return 0
    say("EXPORTED", ITEM_PATH)

    loop = GLib.MainLoop()

    def on_name_acquired(*_args: object) -> None:
        say("NAME", BUS_NAME)
        try:
            connection.call_sync(
                WATCHER_NAME,
                WATCHER_PATH,
                WATCHER_NAME,
                "RegisterStatusNotifierItem",
                GLib.Variant("(s)", (BUS_NAME,)),
                None,
                Gio.DBusCallFlags.NONE,
                5000,
                None,
            )
        except GLib.Error as error:
            say("REGISTER_FAILED", error.message)
            loop.quit()
            return
        say("REGISTERED")

        # Ask the watcher to confirm, rather than trusting that the call
        # returning means anything.
        try:
            reply = connection.call_sync(
                WATCHER_NAME,
                WATCHER_PATH,
                "org.freedesktop.DBus.Properties",
                "Get",
                GLib.Variant("(ss)", (WATCHER_NAME, "RegisteredStatusNotifierItems")),
                None,
                Gio.DBusCallFlags.NONE,
                5000,
                None,
            )
            items = reply.unpack()[0]
        except GLib.Error as error:
            say("READBACK_FAILED", error.message)
        else:
            mine = [item for item in items if BUS_NAME in item]
            say("WATCHER_LISTS", len(items), "MINE", len(mine))

        GLib.timeout_add_seconds(SECONDS, lambda: (loop.quit(), False)[1])

    def on_name_lost(*_args: object) -> None:
        say("NAME_LOST", BUS_NAME)
        loop.quit()

    Gio.bus_own_name_on_connection(
        connection,
        BUS_NAME,
        Gio.BusNameOwnerFlags.NONE,
        on_name_acquired,
        on_name_lost,
    )

    loop.run()
    say("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
