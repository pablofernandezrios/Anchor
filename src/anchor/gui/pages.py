"""The six screens, drawn (SPEC 14).

Each page takes a view model and builds widgets out of it. No page decides
anything: what a button says, whether it is enabled, and why it is not were
all settled next door, where they are tested without a screen.

Every page is rebuilt rather than patched when its data changes. That is a
little more work for the machine and a great deal less for the reader, and it
is how a screen that shows a countdown avoids the class of bug where half of
it is from a minute ago.
"""

from __future__ import annotations

from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from anchor.gui import home as home_model
from anchor.gui import lists as lists_model
from anchor.gui import profiles as profiles_model
from anchor.gui import schedules as schedules_model
from anchor.gui import settings as settings_model
from anchor.gui import stats as stats_model
from anchor.gui.i18n import _
from anchor.gui.outage import Outage
from anchor.gui.widgets import (
    BarChart,
    WeekGrid,
    action_button,
    box,
    card,
    clear,
    describe,
    figure,
    first_icon,
    label,
    pill,
    switch_row,
)


class Page(Gtk.ScrolledWindow):
    """A scrollable screen with a column of content down the middle."""

    def __init__(self) -> None:
        super().__init__(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        self.content = box(spacing=18)
        self.content.set_margin_top(24)
        self.content.set_margin_bottom(24)

        clamp = Adw.Clamp(maximum_size=900, tightening_threshold=700)
        clamp.set_child(self.content)
        clamp.set_margin_start(18)
        clamp.set_margin_end(18)
        self.set_child(clamp)

    def nothing(self, view: Outage) -> None:
        """Draw why there is nothing here, instead of nothing.

        Every screen needs this, so it lives on the base class: a page that
        asked the engine and got no answer must still say something. The toast
        that used to be the whole of it is gone in six seconds and takes the
        explanation with it.
        """
        clear(self.content)
        status = Adw.StatusPage(title=view.title, description=view.detail)
        status.set_icon_name(first_icon("network-offline-symbolic", "dialog-warning-symbolic"))
        self.content.append(status)
        if view.remedy:
            self.content.append(Gtk.Label(label=view.remedy, css_classes=["dim-label"], wrap=True))


class HomePage(Page):
    """Mockup 1: the session, what is coming, and how today went."""

    def __init__(self, act: Any) -> None:
        super().__init__()
        self._act = act

    def show(self, view: home_model.HomeView) -> None:
        clear(self.content)

        if view.banner:
            warning = Adw.Banner(title=view.banner, revealed=True)
            self.content.append(warning)

        if view.session is not None:
            self.content.append(self._session_card(view.session))
        elif view.idle is not None:
            self.content.append(self._idle_card(view.idle))

        if view.schedules:
            self.content.append(self._schedules(view))
        if view.today:
            self.content.append(self._today(view))

    # -- the session -----------------------------------------------------

    def _session_card(self, session: home_model.SessionCard) -> Gtk.Widget:
        holder = card()

        heading = box(Gtk.Orientation.HORIZONTAL, spacing=8)
        heading.append(label(session.profile, css=("title-2",)))
        heading.append(pill(session.level, session.level_key))
        heading.append(label(session.started, css=("dim-label", "caption")))
        holder.append(heading)

        countdown = label(session.countdown, css=("anchor-countdown",))
        holder.append(countdown)
        holder.append(label(session.remaining, css=("dim-label",)))
        describe(countdown, f"{session.countdown} {session.remaining}")

        facts = box(Gtk.Orientation.HORIZONTAL, spacing=24)
        facts.append(self._fact(session.break_title, session.break_when, session.break_detail))
        facts.append(self._fact(_("Blocked attempts"), session.attempts, session.attempts_detail))
        facts.append(self._fact(_("How to leave"), session.exit_how, session.exit_rule))
        holder.append(facts)

        if session.exit_pending:
            holder.append(Adw.Banner(title=session.exit_pending, revealed=True))

        buttons = box(Gtk.Orientation.HORIZONTAL, spacing=8)
        buttons.set_halign(Gtk.Align.END)
        for action in session.actions:
            buttons.append(action_button(action, self._act))
        holder.append(buttons)
        return holder

    def _fact(self, title: str, value: str, detail: str) -> Gtk.Widget:
        holder = box(spacing=2)
        holder.set_hexpand(True)
        holder.append(label(title, css=("caption", "dim-label")))
        holder.append(label(value, css=("heading",), wrap=True))
        if detail:
            holder.append(label(detail, css=("caption", "dim-label"), wrap=True))
        describe(holder, f"{title}: {value}. {detail}".strip())
        return holder

    def _idle_card(self, idle: home_model.IdleCard) -> Gtk.Widget:
        status = Adw.StatusPage(title=idle.title, description=idle.detail)
        status.set_icon_name(first_icon("alarm-symbolic", "document-open-recent-symbolic"))
        status.set_vexpand(False)

        button = action_button(idle.action, self._act)
        button.set_halign(Gtk.Align.CENTER)
        status.set_child(button)
        return status

    # -- the rest --------------------------------------------------------

    def _schedules(self, view: home_model.HomeView) -> Gtk.Widget:
        group = Adw.PreferencesGroup(title=_("Coming up"), description=view.skips)
        for line in view.schedules:
            row = Adw.ActionRow(title=line.name, subtitle=line.when)
            row.add_suffix(pill(line.level, line.level_key))
            if line.now:
                row.add_prefix(Gtk.Image(icon_name="media-playback-start-symbolic"))
            group.add(row)
        return group

    def _today(self, view: home_model.HomeView) -> Gtk.Widget:
        holder = card(_("Today"))
        figures = box(Gtk.Orientation.HORIZONTAL, spacing=12)
        for one in view.today:
            figures.append(figure(one.value, one.label))
        holder.append(figures)
        return holder


class ProfilesPage(Page):
    """Mockup 4: what a profile blocks, and how its breaks behave."""

    def __init__(self, on_change: Any, on_choose: Any) -> None:
        super().__init__()
        self._on_change = on_change
        self._on_choose = on_choose
        self._names: tuple[str, ...] = ()

    def show(
        self,
        screen: profiles_model.ProfileScreen,
        *,
        names: tuple[str, ...] = (),
    ) -> None:
        clear(self.content)
        self._names = names

        if screen.notice:
            self.content.append(Adw.Banner(title=screen.notice, revealed=True))

        self.content.append(self._chooser(screen))
        self.content.append(self._web(screen))
        self.content.append(self._categories(screen))
        self.content.append(self._domains(screen))
        self.content.append(self._apps(screen))
        self.content.append(self._breaks(screen))

    def _chooser(self, screen: profiles_model.ProfileScreen) -> Gtk.Widget:
        group = Adw.PreferencesGroup()
        row = Adw.ComboRow(title=_("Profile"))
        row.set_model(Gtk.StringList.new(list(self._names) or [screen.name]))
        wanted = list(self._names).index(screen.name) if screen.name in self._names else 0
        row.set_selected(wanted)
        row.connect(
            "notify::selected",
            lambda widget, _p: self._on_choose(self._names[widget.get_selected()]),
        )
        group.add(row)
        return group

    def _web(self, screen: profiles_model.ProfileScreen) -> Gtk.Widget:
        group = Adw.PreferencesGroup(title=_("Websites"))
        row = Adw.ComboRow(title=_("Mode"), subtitle=screen.mode_reason or "")
        row.set_model(Gtk.StringList.new([_("Blocklist"), _("Allowlist")]))
        row.set_selected(1 if screen.mode == "allowlist" else 0)
        row.set_sensitive(not screen.mode_locked)
        row.connect(
            "notify::selected",
            lambda widget, _p: self._on_change(
                "web_mode", "allowlist" if widget.get_selected() else "blocklist"
            ),
        )
        group.add(row)

        if screen.mode == "allowlist":
            note = Adw.ActionRow(title=screen.allowlist_warning)
            note.set_subtitle_lines(0)
            note.add_css_class("warning")
            group.add(note)
        return group

    def _categories(self, screen: profiles_model.ProfileScreen) -> Gtk.Widget:
        group = Adw.PreferencesGroup(
            title=_("Categories"), description=_("Websites and apps that go together")
        )
        for toggle in screen.categories:
            group.add(switch_row(toggle, lambda key, on: self._on_change("category", (key, on))))
        return group

    def _domains(self, screen: profiles_model.ProfileScreen) -> Gtk.Widget:
        group = Adw.PreferencesGroup(title=_("Your own domains"))

        add = Adw.EntryRow(title=_("Add a domain"))
        add.set_sensitive(screen.can_add_domains)
        if screen.add_reason:
            add.set_tooltip_text(screen.add_reason)
        add.connect("entry-activated", self._added)
        add.connect("apply", self._added)
        add.set_show_apply_button(True)
        group.add(add)

        for entry in screen.domains:
            row = Adw.ActionRow(title=entry.value)
            remove = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER)
            remove.add_css_class("flat")
            remove.set_sensitive(entry.removable)
            describe(remove, _("Remove {domain}").format(domain=entry.value))
            if entry.reason:
                remove.set_tooltip_text(entry.reason)
            remove.connect(
                "clicked", lambda _b, value=entry.value: self._on_change("remove_domain", value)
            )
            row.add_suffix(remove)
            group.add(row)
        return group

    def _added(self, row: Adw.EntryRow) -> None:
        text = row.get_text().strip()
        if text:
            self._on_change("add_domain", text)
            row.set_text("")

    def _apps(self, screen: profiles_model.ProfileScreen) -> Gtk.Widget:
        group = Adw.PreferencesGroup(
            title=_("Applications"),
            description=_("Tick the installed applications that will be closed"),
        )
        if not screen.apps:
            group.add(Adw.ActionRow(title=_("No applications were found on this machine.")))
        for app in screen.apps:
            row = Adw.SwitchRow(title=app.name, subtitle=app.detail or app.reason)
            row.set_active(app.on)
            row.set_sensitive(not app.locked)
            if app.icon:
                row.add_prefix(Gtk.Image(icon_name=app.icon, pixel_size=32))
            row.connect(
                "notify::active",
                lambda widget, _p, key=app.key: self._on_change("app", (key, widget.get_active())),
            )
            group.add(row)
        return group

    def _breaks(self, screen: profiles_model.ProfileScreen) -> Gtk.Widget:
        group = Adw.PreferencesGroup(title=_("Breaks"))

        group.add(
            self._choice_row(
                _("Pattern"), screen.patterns, screen.pattern, lambda key: ("pattern", key)
            )
        )
        group.add(
            self._choice_row(_("Type"), screen.types, screen.type, lambda key: ("break_type", key))
        )
        group.add(
            self._choice_row(
                _("Hardness"),
                screen.hardnesses,
                screen.hardness,
                lambda key: ("hardness", key),
            )
        )
        group.add(switch_row(screen.allow_sites, lambda key, on: self._on_change(key, on)))
        return group

    def _choice_row(
        self, title: str, choices: tuple[Any, ...], chosen: str, change: Any
    ) -> Adw.ComboRow:
        row = Adw.ComboRow(title=title)
        row.set_model(Gtk.StringList.new([one.title for one in choices]))
        keys = [one.key for one in choices]
        row.set_selected(keys.index(chosen) if chosen in keys else 0)
        row.set_subtitle(next((one.detail for one in choices if one.key == chosen), ""))
        row.connect(
            "notify::selected",
            lambda widget, _p: self._on_change(*change(keys[widget.get_selected()])),
        )
        return row


class SchedulesPage(Page):
    """Mockup 5: the week, and what may be done to it."""

    def __init__(self, act: Any, on_block: Any) -> None:
        super().__init__()
        self._act = act
        self.grid = WeekGrid(on_activate=on_block)

    def show(self, grid: schedules_model.ScheduleGrid) -> None:
        clear(self.content)

        heading = box(Gtk.Orientation.HORIZONTAL, spacing=12)
        heading.append(label(_("Schedules"), css=("title-2",)))
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        heading.append(spacer)
        heading.append(label(grid.skips, css=("dim-label",)))

        new = Gtk.Button(label=_("New schedule"))
        new.add_css_class("suggested-action")
        new.connect("clicked", lambda *_: self._act("schedule.new"))
        heading.append(new)
        self.content.append(heading)

        self.grid.show_week(grid)
        self.content.append(self.grid)

        skip = Gtk.Button(label=grid.skip_label)
        skip.set_sensitive(grid.can_skip)
        skip.set_halign(Gtk.Align.START)
        if grid.skip_reason:
            skip.set_tooltip_text(grid.skip_reason)
        skip.connect("clicked", lambda *_: self._act("schedule.skip"))
        self.content.append(skip)

        self.content.append(label(grid.note, css=("dim-label", "caption"), wrap=True))


class ListsPage(Page):
    """The categories, and what each one covers."""

    def __init__(self, on_change: Any) -> None:
        super().__init__()
        self._on_change = on_change

    def show(self, view: lists_model.ListsView) -> None:
        clear(self.content)

        if not view.categories:
            self.content.append(Adw.StatusPage(title=view.empty))
            return

        self.content.append(label(view.note, css=("dim-label",), wrap=True))
        for entry in view.categories:
            group = Adw.PreferencesGroup(
                title=entry.name, description=f"{entry.summary} · {entry.origin}"
            )
            expander = Adw.ExpanderRow(
                title=_("What it covers"), subtitle=entry.locked_reason or ""
            )
            for item in (*entry.domains, *entry.apps):
                row = Adw.ActionRow(title=item.value)
                remove = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER)
                remove.add_css_class("flat")
                remove.set_sensitive(item.removable)
                describe(remove, _("Remove {entry}").format(entry=item.value))
                if item.reason:
                    remove.set_tooltip_text(item.reason)
                remove.connect(
                    "clicked",
                    lambda _b, key=entry.id, value=item.value: self._on_change(key, value),
                )
                row.add_suffix(remove)
                expander.add_row(row)
            group.add(expander)
            self.content.append(group)


class StatsPage(Page):
    """Mockup 6: how the day, the week or the month actually went."""

    def __init__(self, on_range: Any, on_delete: Any) -> None:
        super().__init__()
        self._on_range = on_range
        self._on_delete = on_delete
        self.chart = BarChart()

    def show(self, view: stats_model.StatsView) -> None:
        clear(self.content)

        heading = box(Gtk.Orientation.HORIZONTAL, spacing=12)
        heading.append(label(view.period, css=("title-2",)))
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        heading.append(spacer)
        heading.append(self._ranges(view))
        self.content.append(heading)

        figures = box(Gtk.Orientation.HORIZONTAL, spacing=12)
        for one in view.headline:
            figures.append(figure(one.value, one.label))
        self.content.append(figures)

        if view.bars:
            holder = card(view.bars_title)
            self.chart.show_bars(view.bars, caption=view.bars_title)
            holder.append(self.chart)
            self.content.append(holder)

        self.content.append(self._attempts(view))
        self.content.append(self._counters(view.breaks_title, view.breaks))
        self.content.append(self._counters(view.ruptures_title, view.ruptures))
        self.content.append(self._delete(view))

    def _ranges(self, view: stats_model.StatsView) -> Gtk.Widget:
        holder = box(Gtk.Orientation.HORIZONTAL, spacing=0, css=("linked",))
        for one in view.ranges:
            button = Gtk.ToggleButton(label=one.title)
            button.set_active(one.key == view.range)
            button.connect("toggled", self._range_toggled, one.key)
            holder.append(button)
        return holder

    def _range_toggled(self, button: Gtk.ToggleButton, key: str) -> None:
        if button.get_active():
            self._on_range(key)

    def _attempts(self, view: stats_model.StatsView) -> Gtk.Widget:
        group = Adw.PreferencesGroup(title=view.attempts_title)
        if not view.attempts:
            group.add(Adw.ActionRow(title=view.attempts_empty))
            return group
        for row in view.attempts:
            entry = Adw.ActionRow(title=row.label)
            entry.add_suffix(label(str(row.count), css=("numeric", "dim-label")))
            group.add(entry)
        return group

    def _counters(self, title: str, figures: tuple[Any, ...]) -> Gtk.Widget:
        holder = card(title)
        row = box(Gtk.Orientation.HORIZONTAL, spacing=12)
        for one in figures:
            row.append(figure(one.value, one.label))
        holder.append(row)
        return holder

    def _delete(self, view: stats_model.StatsView) -> Gtk.Widget:
        group = Adw.PreferencesGroup()
        row = Adw.ActionRow(title=view.delete_label, subtitle=view.delete_warning)
        row.set_subtitle_lines(0)
        button = Gtk.Button(label=view.delete_label, valign=Gtk.Align.CENTER)
        button.add_css_class("destructive-action")
        button.connect("clicked", lambda *_: self._on_delete())
        row.add_suffix(button)
        group.add(row)
        return group


class SettingsPage(Page):
    """The four things a person chooses, and the one thing they can delete."""

    def __init__(self, on_set: Any, on_delete: Any, on_onboarding: Any) -> None:
        super().__init__()
        self._on_set = on_set
        self._on_delete = on_delete
        self._on_onboarding = on_onboarding

    def show(self, view: settings_model.SettingsView) -> None:
        clear(self.content)

        if view.notice:
            self.content.append(Adw.Banner(title=view.notice, revealed=True))

        group = Adw.PreferencesGroup(title=_("Settings"))
        for row in view.rows:
            group.add(self._row(row))
        self.content.append(group)

        introduction = Adw.PreferencesGroup()
        again = Adw.ActionRow(
            title=_("Show the introduction again"),
            subtitle=_("Including the live test that proves blocking works."),
        )
        button = Gtk.Button(label=_("Show"), valign=Gtk.Align.CENTER)
        button.connect("clicked", lambda *_: self._on_onboarding())
        again.add_suffix(button)
        introduction.add(again)
        self.content.append(introduction)

        danger = Adw.PreferencesGroup(title=view.danger_title)
        danger_row = Adw.ActionRow(title=view.delete_label, subtitle=view.delete_warning)
        danger_row.set_subtitle_lines(0)
        delete = Gtk.Button(label=view.delete_label, valign=Gtk.Align.CENTER)
        delete.add_css_class("destructive-action")
        delete.connect("clicked", lambda *_: self._on_delete())
        danger_row.add_suffix(delete)
        danger.add(danger_row)
        self.content.append(danger)

    def _row(self, row: settings_model.Row) -> Gtk.Widget:
        subtitle = row.reason or row.explain
        if row.kind == "choice":
            widget = Adw.ComboRow(title=row.title, subtitle=subtitle)
            widget.set_model(Gtk.StringList.new([one.title for one in row.choices]))
            keys = [one.key for one in row.choices]
            current = str(row.raw or "")
            widget.set_selected(keys.index(current) if current in keys else 0)
            widget.set_sensitive(not row.locked)
            widget.connect(
                "notify::selected",
                lambda combo, _p, key=row.key, keys=keys: self._on_set(
                    key, keys[combo.get_selected()]
                ),
            )
            return widget

        lowest, highest, showing, factor = row.spin
        widget = Adw.SpinRow.new_with_range(lowest, highest, 1)
        widget.set_title(row.title)
        widget.set_subtitle(f"{subtitle} ({row.value})")
        widget.set_value(float(showing))
        widget.set_sensitive(not row.locked)
        widget.connect(
            "notify::value",
            lambda spin, _p, key=row.key, factor=factor: self._on_set(
                key, int(spin.get_value()) * factor
            ),
        )
        return widget
