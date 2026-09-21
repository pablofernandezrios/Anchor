"""Small pieces the screens are built from (SPEC 14).

Nothing here decides anything: every string and every proportion arrives
already worked out from a view model. What is here is the drawing, and the
accessibility that has to travel with it — a bar chart made of boxes says
nothing to a screen reader unless it is told what to say, so each of these
carries its own label (SPEC 14).

Two rules the whole interface follows.

**libadwaita's own colours, never invented ones.** ``accent``, ``warning`` and
``error`` already meet WCAG AA against their backgrounds in both the light and
the dark theme, and they follow the system's choice of the two without Anchor
doing anything. A hand-picked palette would have to be checked twice and would
still be wrong on somebody's high-contrast setting.

**Rebuild rather than mutate.** A screen is redrawn from a fresh view model
every time the status changes. Widgets are cheap and half-updated state is
not: the indicator's own bug — a label that never changed — came from exactly
that kind of partial update.
"""

from __future__ import annotations

from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402  (must follow require_version)

#: The level a session runs at, as a colour libadwaita already contrasts.
LEVEL_STYLE: dict[str, str] = {
    "soft": "accent",
    "firm": "warning",
    "strict": "error",
}


def label(
    text: str,
    *,
    css: str | tuple[str, ...] = (),
    align: Gtk.Align = Gtk.Align.START,
    wrap: bool = False,
    selectable: bool = False,
) -> Gtk.Label:
    """A label, with the style classes it should carry."""
    widget = Gtk.Label(label=text, halign=align, xalign=0.0 if align == Gtk.Align.START else 0.5)
    widget.set_wrap(wrap)
    widget.set_selectable(selectable)
    for name in (css,) if isinstance(css, str) else css:
        widget.add_css_class(name)
    return widget


def box(
    orientation: Gtk.Orientation = Gtk.Orientation.VERTICAL,
    *,
    spacing: int = 6,
    css: str | tuple[str, ...] = (),
    margin: int = 0,
) -> Gtk.Box:
    widget = Gtk.Box(orientation=orientation, spacing=spacing)
    for name in (css,) if isinstance(css, str) else css:
        widget.add_css_class(name)
    if margin:
        for setter in (
            widget.set_margin_top,
            widget.set_margin_bottom,
            widget.set_margin_start,
            widget.set_margin_end,
        ):
            setter(margin)
    return widget


def pill(text: str, level: str) -> Gtk.Label:
    """The level badge: Soft, Firm or Strict, in its own colour."""
    widget = label(text, css=("caption-heading", "anchor-pill", LEVEL_STYLE.get(level, "accent")))
    widget.set_tooltip_text(text)
    return widget


def first_icon(*names: str) -> str:
    """The first of ``names`` this theme can actually draw.

    Icon themes differ, and a name a theme does not have is not refused: GTK
    quietly resolves it to the "missing image" square and draws that. Neither
    of the obvious tests is reliable — ``has_icon`` says yes for names that
    then draw as a square, and a resolved icon out of the theme's cache has no
    file to point at — but what the lookup *resolved to* is honest, because
    GTK renames it to "image-missing" when it gave up.

    So Anchor asks for the icon it wants, and falls back to one every theme
    has rather than showing a broken square. The last name is used whatever
    happens, so the list should end with something certain.
    """
    from gi.repository import Gdk

    display = Gdk.Display.get_default()
    if display is None:  # pragma: no cover - there is always one by now
        return names[-1]

    theme = Gtk.IconTheme.get_for_display(display)
    for name in names[:-1]:
        found = theme.lookup_icon(name, None, 32, 1, Gtk.TextDirection.NONE, 0)
        if found is not None and found.get_icon_name() != "image-missing":
            return name
    return names[-1]


def describe(widget: Gtk.Widget, text: str) -> None:
    """Give a widget the name a screen reader will read out (SPEC 14)."""
    widget.update_property([Gtk.AccessibleProperty.LABEL], [text])


def clear(parent: Gtk.Widget) -> None:
    """Take every child off a box, so a screen can be built again."""
    child = parent.get_first_child()
    while child is not None:
        following = child.get_next_sibling()
        parent.remove(child)
        child = following


def figure(value: str, caption: str) -> Gtk.Box:
    """A big number with what it counts underneath, as the mockups draw it."""
    holder = box(spacing=0)
    holder.set_hexpand(True)
    holder.append(label(value, css=("title-1",), align=Gtk.Align.CENTER))
    holder.append(label(caption, css=("dim-label", "caption"), align=Gtk.Align.CENTER))
    describe(holder, f"{value} {caption}")
    return holder


def card(title: str = "") -> Gtk.Box:
    """A rounded panel, in the style libadwaita gives boxed lists."""
    holder = box(spacing=12, css=("card", "anchor-card"))
    holder.set_margin_top(6)
    holder.set_margin_bottom(6)
    if title:
        holder.append(label(title, css=("heading",)))
    return holder


class BarChart(Gtk.Box):
    """Focus hours per day (mockup 6).

    Boxes rather than a drawing: a ``Gtk.DrawingArea`` would need its own
    Cairo code, its own theme handling and its own accessibility, and a bar
    chart is seven rectangles.
    """

    def __init__(self, *, height: int = 140) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._height = height
        self.set_valign(Gtk.Align.END)

    def show_bars(self, bars: tuple[Any, ...], *, caption: str) -> None:
        clear(self)
        for bar in bars:
            column = box(spacing=4)
            column.set_hexpand(True)
            column.set_valign(Gtk.Align.END)

            filled = Gtk.Box()
            filled.add_css_class("anchor-bar")
            filled.set_size_request(-1, max(2, int(self._height * bar.fraction)))
            filled.set_valign(Gtk.Align.END)

            column.append(label(bar.value, css=("caption", "dim-label"), align=Gtk.Align.CENTER))
            column.append(filled)
            column.append(label(bar.label, css=("caption",), align=Gtk.Align.CENTER))
            describe(column, f"{bar.label}: {bar.value}")
            self.append(column)

        self.set_size_request(-1, self._height + 40)
        describe(self, caption)


class WeekGrid(Gtk.Box):
    """The schedules, as a week (mockup 5).

    Each day is a column and each block is a box with a top margin and a
    height, both worked out next door in :mod:`anchor.gui.schedules`. The
    label says the whole window even when a block is half of one that crosses
    midnight, which is the case this grid exists to get right.
    """

    def __init__(self, *, height: int = 320, on_activate: Any = None) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._height = height
        self._on_activate = on_activate

    def show_week(self, grid: Any) -> None:
        clear(self)

        hours = box(spacing=0)
        hours.set_size_request(56, -1)
        hours.append(label("", css=("caption",)))
        span = max(1, grid.last_minute - grid.first_minute)
        for hour in grid.hours[:-1]:
            slot = label(hour, css=("caption", "dim-label"))
            slot.set_size_request(-1, int(self._height * 60 / span))
            slot.set_valign(Gtk.Align.START)
            hours.append(slot)
        self.append(hours)

        for day in grid.days:
            column = box(spacing=4)
            column.set_hexpand(True)
            heading = label(
                day.name, css=("caption-heading",) if day.today else ("caption", "dim-label")
            )
            heading.set_halign(Gtk.Align.CENTER)
            column.append(heading)

            lane = Gtk.Fixed()
            lane.set_size_request(-1, self._height)
            lane.add_css_class("anchor-lane")
            for block in day.blocks:
                lane.put(self._block(block), 0, self._height * block.offset)
            column.append(lane)
            self.append(column)

        describe(self, grid.note)

    def _block(self, block: Any) -> Gtk.Widget:
        holder = box(spacing=0, css=("anchor-block", LEVEL_STYLE.get(block.level, "accent")))
        holder.set_size_request(120, max(24, int(self._height * block.extent)))
        holder.append(label(block.name, css=("caption-heading",)))
        holder.append(label(block.window, css=("caption",)))

        if not block.enabled:
            holder.add_css_class("dim-label")
        if block.active:
            holder.add_css_class("anchor-now")

        described = f"{block.name}, {block.window}"
        if block.reason:
            described += f". {block.reason}"
        describe(holder, described)
        holder.set_tooltip_text(block.reason or described)

        if self._on_activate is not None:
            press = Gtk.GestureClick()
            press.connect("released", lambda *_: self._on_activate(block))
            holder.add_controller(press)
        return holder


def action_button(action: Any, on_click: Any) -> Gtk.Button:
    """One of the buttons a view model asked for."""
    button = Gtk.Button(label=action.label)
    button.set_sensitive(action.enabled)
    if action.style == "suggested":
        button.add_css_class("suggested-action")
    elif action.style == "destructive":
        button.add_css_class("destructive-action")
    if action.hint:
        button.set_tooltip_text(action.hint)
    describe(button, f"{action.label}. {action.hint}" if action.hint else action.label)
    button.connect("clicked", lambda *_: on_click(action))
    return button


def switch_row(toggle: Any, on_change: Any) -> Adw.SwitchRow:
    """A tick box that knows why it may be locked."""
    row = Adw.SwitchRow(title=toggle.title, subtitle=toggle.detail or toggle.reason)
    row.set_active(toggle.on)
    row.set_sensitive(not toggle.locked)
    if toggle.reason:
        row.set_tooltip_text(toggle.reason)
    row.connect("notify::active", lambda widget, _param: on_change(toggle.key, widget.get_active()))
    return row


CSS = b"""
.anchor-card {
    padding: 18px;
}
.anchor-pill {
    padding: 2px 10px;
    border-radius: 7px;
    background-color: alpha(currentColor, 0.15);
}
.anchor-bar {
    background-image: linear-gradient(to bottom, @accent_bg_color, shade(@accent_bg_color, 0.85));
    border-radius: 6px 6px 0 0;
    min-width: 18px;
}
.anchor-lane {
    background-color: alpha(currentColor, 0.05);
    border-radius: 8px;
}
.anchor-block {
    padding: 6px 8px;
    border-radius: 8px;
    background-color: alpha(currentColor, 0.12);
    border-left: 3px solid currentColor;
}
.anchor-now {
    background-color: alpha(currentColor, 0.28);
}
.anchor-countdown {
    font-size: 2.6rem;
    font-weight: 300;
    font-feature-settings: "tnum";
}
"""


def install_styles(display: Any) -> None:
    """Add Anchor's few style rules to the ones the theme already has."""
    provider = Gtk.CssProvider()
    provider.load_from_data(CSS)
    Gtk.StyleContext.add_provider_for_display(
        display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )
