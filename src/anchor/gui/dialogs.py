"""Start Session, its confirmation, and the first run (SPEC 7.1, 14).

Mockups 2 and 3 are one screen and one dialog: the form has the profile's
defaults already in it, and the dialog reads the consequences back before
anything is enforced. The words in both come from :mod:`anchor.gui.start`;
what is here is the arrangement.

Onboarding is the other one. It is a window rather than a dialog because it
ends by running a real session, and something that takes twenty seconds and
proves a point should not be dismissible by clicking beside it.
"""

from __future__ import annotations

from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from anchor.gui import onboarding as onboarding_model
from anchor.gui import start as start_model
from anchor.gui.i18n import _
from anchor.gui.widgets import box, describe, label


class StartSessionDialog(Adw.Dialog):
    """Mockup 2: one screen, with the profile's defaults preloaded."""

    def __init__(self, *, on_start: Any, on_change: Any) -> None:
        super().__init__(title=_("Start session"), content_width=560, content_height=720)
        self._on_start = on_start
        # The window rebuilds the form on every change, so the rules that
        # decide what may be started live in one tested place rather than
        # being scattered across the widgets that happened to change.
        self._on_change = on_change
        self._form: start_model.StartForm | None = None

        self._toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self._toolbar.add_top_bar(header)

        self._start = Gtk.Button(label=_("Review and start"))
        self._start.add_css_class("suggested-action")
        self._start.connect("clicked", lambda *_: self._review())
        header.pack_end(self._start)
        self.set_child(self._toolbar)

    def show(self, form: start_model.StartForm) -> None:
        """Draw the form again. Called on every change the user makes."""
        self._form = form
        # A fresh page rather than emptying the old one: the children of an
        # Adw.PreferencesPage are its own scrolled window and box, not the
        # groups that were added, so taking them off by hand takes the wrong
        # thing apart.
        page = Adw.PreferencesPage()
        page.add(self._profile(form))
        page.add(self._duration(form))
        page.add(self._levels(form))
        if form.valves:
            page.add(self._valves(form))
        page.add(self._breaks(form))
        self._toolbar.set_content(page)

        self._start.set_sensitive(form.can_start)
        self._start.set_tooltip_text(form.problem)

    # -- the parts -------------------------------------------------------

    def _profile(self, form: start_model.StartForm) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title=_("Profile"))
        row = Adw.ComboRow(title=_("Profile"), subtitle=form.blocks)
        row.set_model(Gtk.StringList.new(list(form.profiles)))
        names = list(form.profiles)
        row.set_selected(names.index(form.profile) if form.profile in names else 0)
        row.connect(
            "notify::selected",
            lambda widget, _p: self._changed("profile", names[widget.get_selected()]),
        )
        group.add(row)
        return group

    def _duration(self, form: start_model.StartForm) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title=_("Duration"), description=form.cap)
        row = Adw.ActionRow(title=form.duration, subtitle=form.ends)

        for name, step in ((_("Shorter"), -1), (_("Longer"), +1)):
            button = Gtk.Button(
                icon_name="list-remove-symbolic" if step < 0 else "list-add-symbolic",
                valign=Gtk.Align.CENTER,
            )
            button.add_css_class("flat")
            describe(button, name)
            button.connect(
                "clicked",
                lambda _b, step=step: self._changed(
                    "duration",
                    start_model.stepped(self._form.duration_seconds if self._form else 0, step),
                ),
            )
            row.add_suffix(button)

        group.add(row)
        if form.problem:
            problem = Adw.ActionRow(title=form.problem)
            problem.add_css_class("error")
            group.add(problem)
        return group

    def _levels(self, form: start_model.StartForm) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title=_("Blocking level"))
        first: Gtk.CheckButton | None = None
        for choice in form.levels:
            row = Adw.ActionRow(title=choice.title, subtitle=choice.detail)
            row.set_subtitle_lines(0)
            button = Gtk.CheckButton(valign=Gtk.Align.CENTER)
            if first is None:
                first = button
            else:
                button.set_group(first)
            button.set_active(choice.key == form.level)
            button.connect("toggled", self._picked, "level", choice.key)
            row.add_prefix(button)
            row.set_activatable_widget(button)
            group.add(row)
        return group

    def _valves(self, form: start_model.StartForm) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title=_("Emergency valve"),
            description=_("Chosen now, because a Strict session cannot be cancelled."),
        )
        first: Gtk.CheckButton | None = None
        for choice in form.valves:
            row = Adw.ActionRow(title=choice.title, subtitle=choice.detail)
            row.set_subtitle_lines(0)
            button = Gtk.CheckButton(valign=Gtk.Align.CENTER)
            if first is None:
                first = button
            else:
                button.set_group(first)
            button.set_active(choice.key == form.valve)
            button.connect("toggled", self._picked, "valve", choice.key)
            row.add_prefix(button)
            row.set_activatable_widget(button)
            group.add(row)
        return group

    def _breaks(self, form: start_model.StartForm) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title=_("Breaks"))
        group.add(Adw.ActionRow(title=form.breaks, subtitle=form.breaks_hint))
        return group

    # -- what the buttons do ---------------------------------------------

    def _picked(self, button: Gtk.CheckButton, what: str, key: str) -> None:
        """One radio button of a group. Only the one being turned on speaks."""
        if button.get_active():
            self._changed(what, key)

    def _changed(self, what: str, value: Any) -> None:
        self._on_change(what, value)

    def _review(self) -> None:
        if self._form is None or not self._form.can_start:
            return
        confirmation(self._form, parent=self, on_confirm=self._confirmed).present(self)

    def _confirmed(self) -> None:
        if self._form is not None:
            self._on_start(self._form)
        self.close()


def confirmation(form: start_model.StartForm, *, parent: Any, on_confirm: Any) -> Adw.AlertDialog:
    """Mockup 3: what is about to happen, before it happens (SPEC 7.1)."""
    said = start_model.confirmation(form)

    lines = [said.profile_line, said.ends_line, said.exit_line]
    lines += [line for line in (said.tunnels_line, said.web_line, said.apps_line) if line]

    dialog = Adw.AlertDialog(heading=said.title, body="\n\n".join(lines))
    dialog.add_response("back", said.back)
    dialog.add_response("start", said.confirm)
    dialog.set_response_appearance("start", Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response("back")
    dialog.set_close_response("back")
    dialog.connect("response", lambda _d, response: on_confirm() if response == "start" else None)
    del parent
    return dialog


class OnboardingWindow(Adw.Window):
    """First run: what Anchor is, and proof that it works (SPEC 14)."""

    def __init__(self, *, parent: Any, run_test: Any, on_finished: Any, indicator: bool | None):
        # Given to the application as well as to the window it belongs to: a
        # window GTK does not know about is one nothing can find again, the
        # interface itself included.
        super().__init__(
            application=parent.get_application(),
            # Without a title of its own a window borrows the program's name,
            # which on a checkout is "python3".
            title="Anchor",
            transient_for=parent,
            modal=True,
            default_width=620,
            default_height=560,
        )
        self._run_test = run_test
        self._on_finished = on_finished

        self._steps = [
            *onboarding_model.STEPS[:-1],
            onboarding_model.indicator_step(indicator),
            onboarding_model.STEPS[-1],
        ]
        self._at = 0
        self._tested = False

        self._title = label("", css=("title-1",), wrap=True)
        self._body = label("", css=("body",), wrap=True)
        self._points = box(spacing=8)
        self._status = box(spacing=8)

        content = box(spacing=18)
        content.set_margin_top(36)
        content.set_margin_bottom(24)
        content.set_margin_start(36)
        content.set_margin_end(36)
        content.append(self._title)
        content.append(self._body)
        content.append(self._points)
        content.append(self._status)

        self._next = Gtk.Button(label=_("Next"))
        self._next.add_css_class("suggested-action")
        self._next.connect("clicked", lambda *_: self._advance())

        buttons = box(Gtk.Orientation.HORIZONTAL, spacing=8)
        buttons.set_halign(Gtk.Align.END)
        buttons.set_margin_end(36)
        buttons.set_margin_bottom(24)
        buttons.append(self._next)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar(show_end_title_buttons=False))
        holder = box(spacing=0)
        holder.append(content)
        spacer = Gtk.Box()
        spacer.set_vexpand(True)
        holder.append(spacer)
        holder.append(buttons)
        toolbar.set_content(holder)
        self.set_content(toolbar)

        self._draw()

    def _draw(self) -> None:
        step = self._steps[self._at]
        self._title.set_text(step.title)
        self._body.set_text(step.body)
        self._body.set_visible(bool(step.body))

        child = self._points.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self._points.remove(child)
            child = following
        for point in step.points:
            line = box(Gtk.Orientation.HORIZONTAL, spacing=8)
            line.append(Gtk.Image(icon_name="object-select-symbolic", valign=Gtk.Align.START))
            line.append(label(point, wrap=True))
            self._points.append(line)

        last = self._at == len(self._steps) - 1
        self._next.set_label(_("Run the test") if last else _("Next"))

    def _advance(self) -> None:
        """Next page, then the test, then done. One button, three jobs."""
        if self._tested:
            self._on_finished()
            self.close()
            return
        if self._at < len(self._steps) - 1:
            self._at += 1
            self._draw()
            return
        self._next.set_sensitive(False)
        self._next.set_label(_("Testing…"))
        self._run_test(self._show_result)

    def _show_result(self, result: onboarding_model.TestResult) -> None:
        child = self._status.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self._status.remove(child)
            child = following

        self._status.append(label(result.title, css=("title-4",), wrap=True))
        self._status.append(label(result.detail, wrap=True))
        if result.fix:
            self._status.append(label(result.fix, css=("dim-label",), wrap=True))

        self._tested = True
        self._next.set_sensitive(True)
        self._next.set_label(_("Finish"))
