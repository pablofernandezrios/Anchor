"""The desktop side of the agent: the top bar and the notification server.

This is the only module in Anchor that talks to the session bus, and it is
deliberately thin. Everything it could get wrong in an interesting way — what
the label says, when to stay quiet, what happens when the engine goes away —
was decided next door in :mod:`indicator` and :mod:`notifications`, where it
can be tested without a screen. What is left here is plumbing, and plumbing is
checked by running it on a real desktop (``tools/check_agent.py``).

The indicator implements ``org.kde.StatusNotifierItem`` directly rather than
through ``libayatana-appindicator``, which is a GTK3 library and cannot share a
process with Anchor's GTK4 interface: see ADR 3. Milestone 0 spike 4 confirmed
on Ubuntu GNOME that an item registered this way is accepted, listed back, and
shows its label beside the icon.

PyGObject is imported here and nowhere else in the agent, so a machine without
it can still run everything except the drawing.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from anchor.agent.indicator import IndicatorView
from anchor.agent.notifications import Notification

log = logging.getLogger("anchor-agent")

WATCHER_NAME = "org.kde.StatusNotifierWatcher"
WATCHER_PATH = "/StatusNotifierWatcher"
ITEM_INTERFACE = "org.kde.StatusNotifierItem"
ITEM_PATH = "/StatusNotifierItem"

NOTIFICATIONS_NAME = "org.freedesktop.Notifications"
NOTIFICATIONS_PATH = "/org/freedesktop/Notifications"

#: What the panel reads. ``XAyatanaLabel`` is how the remaining time gets
#: beside the icon rather than only inside the menu (SPEC 14.1).
ITEM_XML = """
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
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="XAyatanaLabel" type="s" access="read"/>
    <property name="XAyatanaLabelGuide" type="s" access="read"/>
    <method name="Activate">
      <arg type="i" name="x" direction="in"/>
      <arg type="i" name="y" direction="in"/>
    </method>
    <method name="SecondaryActivate">
      <arg type="i" name="x" direction="in"/>
      <arg type="i" name="y" direction="in"/>
    </method>
    <method name="Scroll">
      <arg type="i" name="delta" direction="in"/>
      <arg type="s" name="orientation" direction="in"/>
    </method>
    <signal name="NewIcon"/>
    <signal name="NewTitle"/>
    <signal name="NewToolTip"/>
    <signal name="NewStatus"><arg type="s" name="status"/></signal>
    <signal name="XAyatanaNewLabel">
      <arg type="s" name="label"/>
      <arg type="s" name="guide"/>
    </signal>
  </interface>
</node>
"""


#: The arguments each signal carries, as a D-Bus signature. ``None`` means the
#: signal has none and the watcher re-reads the properties itself.
SIGNAL_TYPES: dict[str, str | None] = {
    "NewIcon": None,
    "NewTitle": None,
    "NewToolTip": None,
    "NewStatus": "(s)",
    # Named after its property, not "NewLabel". The panel turns a signal name
    # into a property name by taking off the "New" — or, for this one, the
    # "XAyatanaNew" — so a signal called NewLabel asks it to re-read a property
    # called Label, which does not exist, and the label never changes. This is
    # what the first run on a real desktop found, and spike 4 could not: its
    # label never moved.
    "XAyatanaNewLabel": "(ss)",
}


def signals_for(before: IndicatorView, after: IndicatorView) -> list[tuple[str, tuple[str, ...]]]:
    """Which signals a change from ``before`` to ``after`` needs.

    Only what changed. A panel that is told everything changed re-reads
    everything, every second, for the whole session.
    """
    changes: list[tuple[str, tuple[str, ...]]] = []
    if before.icon != after.icon:
        changes.append(("NewIcon", ()))
    if before.title != after.title:
        changes.append(("NewTitle", ()))
    if before.label != after.label or before.guide != after.guide:
        changes.append(("XAyatanaNewLabel", (after.label, after.guide)))
    if before.tooltip != after.tooltip:
        changes.append(("NewToolTip", ()))
    if before.visible != after.visible:
        changes.append(("NewStatus", (status_word(after),)))
    return changes


def status_word(view: IndicatorView) -> str:
    """``Active`` while a session runs, ``Passive`` when the item is hidden."""
    return "Active" if view.visible else "Passive"


class DesktopUnavailableError(RuntimeError):
    """There is no session bus, or no PyGObject, so nothing can be drawn."""


def _gi() -> Any:
    """Import PyGObject, or explain what is missing.

    Imported here rather than at the top of the module so that the agent's
    tests, and ``--dry-run``, work on a machine with no desktop at all.
    """
    try:
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib
    except (ImportError, ValueError) as error:
        raise DesktopUnavailableError(
            f"PyGObject is not available ({error}). Install python3-gi."
        ) from error
    return Gio, GLib


def session_bus() -> Any:
    """The session bus, or a clear refusal."""
    gio, glib = _gi()
    try:
        return gio.bus_get_sync(gio.BusType.SESSION, None)
    except glib.Error as error:
        raise DesktopUnavailableError(
            f"cannot reach the session bus ({error.message}). The agent is a user "
            "service and needs the user's own session."
        ) from error


class TrayItem:
    """Anchor's entry in the top bar (SPEC 14.1, ADR 3).

    One item is exported for the life of the agent and its ``Status`` switches
    between ``Active`` and ``Passive``. Registering and unregistering with the
    watcher on every session start would be more literal and much worse: some
    panels forget an item that leaves, and the user would be left with a bar
    that stayed empty until they logged out.
    """

    def __init__(self, connection: Any = None, *, item_id: str = "anchor") -> None:
        self._gio, self._glib = _gi()
        self._connection = connection if connection is not None else session_bus()
        self._id = item_id
        self._bus_name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        self._view = IndicatorView()
        self._registration: int | None = None
        self._name_id: int | None = None
        self._watch_id: int | None = None
        self._on_activate: Any = None

    # -- lifecycle -------------------------------------------------------

    def start(self, on_activate: Any = None) -> None:
        """Export the item and offer it to whatever is watching."""
        self._on_activate = on_activate
        node = self._gio.DBusNodeInfo.new_for_xml(ITEM_XML)
        self._registration = self._connection.register_object(
            ITEM_PATH, node.interfaces[0], self._call, self._get_property, None
        )
        self._name_id = self._gio.bus_own_name_on_connection(
            self._connection,
            self._bus_name,
            self._gio.BusNameOwnerFlags.NONE,
            lambda *_: self._register_with_watcher(),
            lambda *_: log.warning("lost the bus name %s", self._bus_name),
        )
        # The watcher comes and goes: the extension can be disabled, enabled,
        # or arrive after the agent. Registering once at startup would leave
        # the indicator missing for the rest of the login.
        self._watch_id = self._gio.bus_watch_name_on_connection(
            self._connection,
            WATCHER_NAME,
            self._gio.BusNameWatcherFlags.NONE,
            lambda *_: self._register_with_watcher(),
            lambda *_: log.info("no StatusNotifierWatcher; the top bar has no tray"),
        )

    def stop(self) -> None:
        if self._watch_id is not None:
            self._gio.bus_unwatch_name(self._watch_id)
            self._watch_id = None
        if self._name_id is not None:
            self._gio.bus_unown_name(self._name_id)
            self._name_id = None
        if self._registration is not None:
            self._connection.unregister_object(self._registration)
            self._registration = None

    def _register_with_watcher(self) -> None:
        try:
            self._connection.call_sync(
                WATCHER_NAME,
                WATCHER_PATH,
                WATCHER_NAME,
                "RegisterStatusNotifierItem",
                self._glib.Variant("(s)", (self._bus_name,)),
                None,
                self._gio.DBusCallFlags.NONE,
                5000,
                None,
            )
        except self._glib.Error as error:
            # Not fatal, and not worth a warning on a desktop that has no tray:
            # the remaining time is still in `anchor status` and in the
            # interface. Onboarding is where SPEC 14.1 says to explain this.
            log.info("no tray to register with: %s", error.message)
            return
        log.info("registered %s with %s", self._bus_name, WATCHER_NAME)

    # -- what the panel sees ---------------------------------------------

    def show(self, view: IndicatorView) -> None:
        """Take a new view, and tell the panel only what changed."""
        before, self._view = self._view, view
        for signal, arguments in signals_for(before, view):
            signature = SIGNAL_TYPES[signal]
            body = self._glib.Variant(signature, arguments) if signature else None
            self._emit(signal, body)

    def _status(self) -> str:
        return status_word(self._view)

    def _emit(self, signal: str, body: Any = None) -> None:
        try:
            self._connection.emit_signal(None, ITEM_PATH, ITEM_INTERFACE, signal, body)
        except self._glib.Error as error:
            log.debug("could not emit %s: %s", signal, error.message)

    def _get_property(
        self, _connection: Any, _sender: str, _path: str, _interface: str, name: str
    ) -> Any:
        glib = self._glib
        view = self._view
        match name:
            case "Category":
                return glib.Variant("s", "ApplicationStatus")
            case "Id":
                return glib.Variant("s", self._id)
            case "Title":
                return glib.Variant("s", view.title)
            case "Status":
                return glib.Variant("s", self._status())
            case "IconName":
                return glib.Variant("s", view.icon)
            case "IconThemePath":
                return glib.Variant("s", "")
            case "ItemIsMenu":
                # False, so a click reaches Activate rather than waiting for a
                # menu. The menu itself arrives with the interface (ADR 3).
                return glib.Variant("b", False)
            case "Menu":
                return glib.Variant("o", "/MenuBar")
            case "ToolTip":
                return glib.Variant("(sa(iiay)ss)", (view.icon, [], view.title, view.tooltip))
            case "XAyatanaLabel":
                return glib.Variant("s", view.label)
            case "XAyatanaLabelGuide":
                return glib.Variant("s", view.guide)
        return None

    def _call(
        self,
        _connection: Any,
        _sender: str,
        _path: str,
        _interface: str,
        method: str,
        _parameters: Any,
        invocation: Any,
    ) -> None:
        if method in ("Activate", "SecondaryActivate") and self._on_activate is not None:
            try:
                self._on_activate()
            except Exception:
                log.exception("the activation handler raised")
        invocation.return_value(None)


class DesktopNotifier:
    """Notifications through ``org.freedesktop.Notifications`` (SPEC 8.3)."""

    def __init__(self, connection: Any = None, *, app_name: str = "Anchor") -> None:
        self._gio, self._glib = _gi()
        self._connection = connection if connection is not None else session_bus()
        self._app_name = app_name
        self._ids: dict[str, int] = {}

    def send(self, notification: Notification) -> None:
        glib = self._glib
        replaces = self._ids.get(notification.channel, 0) if notification.channel else 0

        arguments = glib.Variant(
            "(susssasa{sv}i)",
            (
                self._app_name,
                replaces,
                notification.icon,
                notification.summary,
                notification.body,
                [],
                {"urgency": glib.Variant("y", int(notification.urgency))},
                notification.timeout_ms,
            ),
        )
        try:
            reply = self._connection.call_sync(
                NOTIFICATIONS_NAME,
                NOTIFICATIONS_PATH,
                NOTIFICATIONS_NAME,
                "Notify",
                arguments,
                glib.VariantType("(u)"),
                self._gio.DBusCallFlags.NONE,
                5000,
                None,
            )
        except self._glib.Error as error:
            # A desktop with no notification server is a worse desktop, not a
            # broken Anchor: the blocking it is reporting already happened.
            log.info("could not show a notification: %s", error.message)
            return

        if notification.channel:
            self._ids[notification.channel] = int(reply.unpack()[0])
