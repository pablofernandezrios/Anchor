# 3. Speak StatusNotifierItem over D-Bus instead of using AppIndicator

- **Status:** Accepted
- **Date:** 2026-09-20
- **Evidence:** Milestone 0 spike 4, Ubuntu GNOME on Wayland
- **Affects:** SPEC 5.1, 14.1, and the dependency lists in SPEC 17

## Context

SPEC 14.1 says the top-bar indicator uses StatusNotifierItem/AppIndicator, and
SPEC 5.1 puts the indicator inside `anchor-agent`, alongside the notifications
and the fullscreen break overlay.

The obvious way to do that is `libayatana-appindicator`, through the
`AyatanaAppIndicator3` typelib. Spike 4 found the problem with it: that library
is **GTK3**, and Anchor's interface is **GTK4** (SPEC 14). A single process
cannot load GTK3 and GTK4 at the same time. Using it would mean either
rewriting the agent around GTK3, which rules out sharing anything with the
GTK4 interface, or splitting the indicator into a separate process, which
contradicts SPEC 5.1.

Neither is necessary. GNOME's AppIndicator extension does not care what library
produced an item; it watches `org.kde.StatusNotifierWatcher` on the session bus
and shows whatever registers there.

## Decision

`anchor-agent` implements `org.kde.StatusNotifierItem` itself, over D-Bus,
using `Gio` from PyGObject. No AppIndicator library is involved.

Spike 4 confirmed on Ubuntu GNOME on Wayland that an item registered this way
is accepted by the watcher and listed back by it, with the properties SPEC 14.1
needs: the icon, the title, and `XAyatanaLabel` for the remaining time beside
the icon.

`gir1.2-ayatanaappindicator3-0.1` and its equivalents leave the package
dependencies. PyGObject was already required for the interface.

## Consequences

- The indicator stays part of `anchor-agent`, as SPEC 5.1 describes. Nothing
  about the architecture changes.
- One fewer dependency, and no GTK3 anywhere in Anchor.
- The menu is more work. `libayatana-appindicator` provides `com.canonical.dbusmenu`
  for free; implementing the interface directly means exporting that menu
  ourselves. SPEC 14.1 lists what the menu holds, so the shape is known.
  **Paid in Milestone 9**: `anchor/agent/menu.py` arranges the items and
  `TrayMenu` in `anchor/agent/desktop.py` answers the panel's questions about
  them. It came to about 150 lines, and the one part worth knowing is the
  revision number: a panel caches the layout and only asks again when told
  the revision moved, so a menu that changes without saying so is a menu that
  never changes on screen.
- Whether the `XAyatanaLabel` text renders is the extension's decision, not
  Anchor's. If an extension shows only the icon, the remaining time is still on
  the Home screen and in `anchor status`, and onboarding can say so.
- This is not a deviation from SPEC 14.1: StatusNotifierItem is exactly what
  the specification names. Only the library used to speak it changes.
