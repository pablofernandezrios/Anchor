"""The window, and what it asks the engine for (SPEC 14).

One window with six pages, a view switcher in the header as the mockups draw
it, and a toast whenever the engine says no. Everything the screens show comes
from a view model; everything they do goes back as a request.

Three rules this file keeps.

**Nothing is decided here.** A refusal from the engine becomes a toast with
the engine's own sentence in it. The interface never second-guesses it and
never pretends an action worked.

**Redraw from the engine, not from hope.** After any request that changes
something, the page asks again rather than editing what is on screen. It costs
a round trip over a local socket and it means the window cannot drift away
from what is actually enforced.

**The countdown ticks even when the engine is quiet.** The engine sends a tick
every second while a session runs, which is what Home shows; when it goes
quiet the banner says so rather than the numbers freezing silently.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from anchor.blocker.apps import discover
from anchor.gui import home as home_model
from anchor.gui import lists as lists_model
from anchor.gui import onboarding as onboarding_model
from anchor.gui import profiles as profiles_model
from anchor.gui import schedules as schedules_model
from anchor.gui import settings as settings_model
from anchor.gui import start as start_model
from anchor.gui import stats as stats_model
from anchor.gui.dialogs import OnboardingWindow, StartSessionDialog
from anchor.gui.engine import EngineLink, Reply
from anchor.gui.i18n import N_, _
from anchor.gui.pages import (
    HomePage,
    ListsPage,
    ProfilesPage,
    SchedulesPage,
    SettingsPage,
    StatsPage,
)
from anchor.gui.widgets import first_icon, install_styles

log = logging.getLogger("anchor-gui")

APP_ID = "org.anchor.Anchor"

#: The pages, in the order the mockups put them in the navigation bar, with
#: the icons each one would like. More than one name because icon themes
#: differ and a name a theme lacks draws as a broken square: `first_icon`
#: takes the best one that this theme can actually draw, and the last name of
#: each row is one every theme has.
PAGES = (
    ("home", N_("Home"), ("user-home-symbolic", "go-home-symbolic")),
    ("profiles", N_("Profiles"), ("view-list-bullet-symbolic", "view-grid-symbolic")),
    (
        "schedules",
        N_("Schedules"),
        ("x-office-calendar-symbolic", "alarm-symbolic", "document-open-recent-symbolic"),
    ),
    ("lists", N_("Lists"), ("view-list-symbolic",)),
    (
        "stats",
        N_("Statistics"),
        ("x-office-spreadsheet-symbolic", "org.gnome.Settings-symbolic", "view-grid-symbolic"),
    ),
    ("settings", N_("Settings"), ("preferences-system-symbolic", "emblem-system-symbolic")),
)


class AnchorWindow(Adw.ApplicationWindow):
    """Anchor's one window."""

    def __init__(self, application: Adw.Application, link: EngineLink) -> None:
        super().__init__(application=application, default_width=1000, default_height=720)
        self.set_title("Anchor")
        self.link = link

        self.status: dict[str, Any] = {}
        self.profile_name = ""
        self.profile: dict[str, Any] = {}
        self.profile_names: tuple[str, ...] = ()
        self.categories: list[dict[str, Any]] = []
        self.stats_range = "week"
        self.installed = self._installed_apps()
        self._start_dialog: StartSessionDialog | None = None
        self._form: start_model.StartForm | None = None

        self.toasts = Adw.ToastOverlay()
        self.stack = Adw.ViewStack()
        self._build_pages()

        switcher = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        header = Adw.HeaderBar()
        header.set_title_widget(switcher)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.add_bottom_bar(Adw.ViewSwitcherBar(stack=self.stack, reveal=False))
        toolbar.set_content(self.stack)

        self.toasts.set_child(toolbar)
        self.set_content(self.toasts)

        self.stack.connect("notify::visible-child-name", lambda *_: self.refresh())
        link.watch_status(self._took_status)
        link.watch_connection(lambda *_: self.refresh())
        link.watch_events(lambda *_: self._on_event())

    # -- building --------------------------------------------------------

    def _build_pages(self) -> None:
        self.home = HomePage(self._act)
        self.profiles = ProfilesPage(self._profile_changed, self._choose_profile)
        self.schedules = SchedulesPage(self._act, self._edit_schedule)
        self.lists = ListsPage(self._remove_from_category)
        self.statistics = StatsPage(self._choose_range, self._delete_statistics)
        self.settings = SettingsPage(
            self._set_setting, self._delete_statistics, self.show_onboarding
        )

        for (name, title, icons), page in zip(
            PAGES,
            (
                self.home,
                self.profiles,
                self.schedules,
                self.lists,
                self.statistics,
                self.settings,
            ),
            strict=True,
        ):
            self.stack.add_titled_with_icon(page, name, _(title), first_icon(*icons))

    def _installed_apps(self) -> list[dict[str, Any]]:
        """The applications this machine has (SPEC 9).

        Read here rather than asked of the engine: ``.desktop`` files need no
        privilege, and the interface already runs as the user whose menu they
        describe.
        """
        try:
            return [{"id": app.id, "name": app.name, "icon": app.icon} for app in discover()]
        except OSError as error:
            log.warning("could not list the installed applications: %s", error)
            return []

    # -- what the engine says --------------------------------------------

    def _took_status(self, status: dict[str, Any]) -> None:
        was_active = bool(self.status.get("active"))
        self.status = status
        if bool(status.get("active")) != was_active:
            # A session started or ended: every page's answer changes.
            self.refresh()
            return
        if self.stack.get_visible_child_name() == "home":
            self._draw_home()

    def _on_event(self) -> None:
        if self.stack.get_visible_child_name() in ("home", "schedules"):
            self.refresh()

    # -- drawing ---------------------------------------------------------

    def refresh(self) -> None:
        """Ask for what the visible page needs, then draw it."""
        page = self.stack.get_visible_child_name() or "home"
        getattr(self, f"_load_{page}")()

    def _load_home(self) -> None:
        self.link.ask_all(
            [("status.get", {}), ("schedule.list", {}), ("stats.query", {"range": "day"})],
            self._home_loaded,
        )

    def _home_loaded(self, reply: Reply) -> None:
        if reply.code == "UNREACHABLE":
            self._draw_home()
            return
        self.status = reply.result.get("status.get", self.status)
        self._schedule_listing = reply.result.get("schedule.list", {})
        self._today = reply.result.get("stats.query", {})
        self._draw_home()

    def _draw_home(self) -> None:
        self.home.show(
            home_model.home_view(
                self.status,
                schedules=getattr(self, "_schedule_listing", {}),
                stats=getattr(self, "_today", {}),
                connected=self.link.connected,
                apps={app["id"]: app["name"] for app in self.installed},
            )
        )

    def _load_profiles(self) -> None:
        self.link.ask_all([("profile.list", {}), ("category.list", {})], self._profiles_loaded)

    def _profiles_loaded(self, reply: Reply) -> None:
        self.profile_names = tuple(reply.result.get("profile.list", {}).get("profiles", ()))
        self.categories = list(reply.result.get("category.list", {}).get("categories", []))
        if not self.profile_names:
            return
        if self.profile_name not in self.profile_names:
            self.profile_name = self.profile_names[0]
        self.link.ask("profile.show", {"name": self.profile_name}, self._profile_loaded)

    def _profile_loaded(self, reply: Reply) -> None:
        if not reply.ok:
            self._complain(reply)
            return
        self.profile = reply.result.get("profile", {})
        self.profiles.show(
            profiles_model.profile_view(
                profile=self.profile,
                categories=self.categories,
                apps=self.installed,
                in_use=self._profile_in_use(),
            ),
            names=self.profile_names,
        )

    def _profile_in_use(self) -> bool:
        return bool(self.status.get("active")) and self.status.get("profile") == self.profile_name

    def _load_schedules(self) -> None:
        self.link.ask("schedule.list", {}, self._schedules_loaded)

    def _schedules_loaded(self, reply: Reply) -> None:
        if not reply.ok:
            self._complain(reply)
            return
        self.schedules.show(
            schedules_model.schedule_grid(
                listing=reply.result,
                status=self.status,
                today=datetime.now().weekday(),
            )
        )

    def _load_lists(self) -> None:
        self.link.ask("category.list", {}, self._lists_loaded)

    def _lists_loaded(self, reply: Reply) -> None:
        if not reply.ok:
            self._complain(reply)
            return
        self.categories = list(reply.result.get("categories", []))
        self.lists.show(
            lists_model.lists_view(self.categories, blocked_now=self._blocked_categories())
        )

    def _blocked_categories(self) -> set[str]:
        if not self.status.get("active"):
            return set()
        if self.profile.get("name") == self.status.get("profile"):
            return set(self.profile.get("categories") or ())
        # The running session's profile has not been fetched, so nothing is
        # claimed. A wrong "locked" badge is worse than none.
        return set()

    def _load_stats(self) -> None:
        self.link.ask("stats.query", {"range": self.stats_range}, self._stats_loaded)

    def _stats_loaded(self, reply: Reply) -> None:
        if not reply.ok:
            self._complain(reply)
            return
        self.statistics.show(
            stats_model.stats_view(
                summary=reply.result,
                apps={app["id"]: app["name"] for app in self.installed},
            )
        )

    def _load_settings(self) -> None:
        self.link.ask("config.get", {}, self._settings_loaded)

    def _settings_loaded(self, reply: Reply) -> None:
        if not reply.ok:
            self._complain(reply)
            return
        self.settings.show(
            settings_model.settings_view(
                settings=reply.result.get("settings", []),
                in_session=bool(self.status.get("active")),
            )
        )

    # -- what the buttons do ---------------------------------------------

    def _act(self, action: Any) -> None:
        request = action if isinstance(action, str) else action.request

        if request == "session.start":
            self.show_start_session()
            return
        if request == "schedule.new":
            self._toast(_("Creating schedules from here arrives with packaging."))
            return
        if request == "session.extend":
            self._extend()
            return
        self.link.ask(request, {}, self._acted)

    def _acted(self, reply: Reply) -> None:
        if not reply.ok:
            self._complain(reply)
            return
        self.refresh()

    def _extend(self) -> None:
        dialog = Adw.AlertDialog(
            heading=_("Extend the session"),
            body=_("Any amount. A session can be extended but never shortened."),
        )
        spin = Gtk.SpinButton.new_with_range(5, 240, 5)
        spin.set_value(30)
        dialog.set_extra_child(spin)
        dialog.add_response("back", _("Cancel"))
        dialog.add_response("extend", _("Extend"))
        dialog.set_response_appearance("extend", Adw.ResponseAppearance.SUGGESTED)
        dialog.connect(
            "response",
            lambda _d, response: (
                self.link.ask(
                    "session.extend",
                    {"by_seconds": int(spin.get_value()) * 60},
                    self._acted,
                )
                if response == "extend"
                else None
            ),
        )
        dialog.present(self)

    # -- start session ---------------------------------------------------

    def show_start_session(self) -> None:
        self.link.ask_all([("profile.list", {})], self._start_profiles_loaded)

    def _start_profiles_loaded(self, reply: Reply) -> None:
        names = tuple(reply.result.get("profile.list", {}).get("profiles", ()))
        if not names:
            self._toast(_("There are no profiles yet."))
            return
        self._start_names = names
        self.link.ask("profile.show", {"name": names[0]}, self._start_profile_loaded)

    def _start_profile_loaded(self, reply: Reply) -> None:
        if not reply.ok:
            self._complain(reply)
            return
        self._start_profile = reply.result.get("profile", {})
        self._form = start_model.start_form(
            profiles=list(self._start_names),
            profile=self._start_profile,
            duration_seconds=90 * 60,
            level="firm",
            valve=None,
            now=datetime.now().timestamp(),
            apps={app["id"]: app["name"] for app in self.installed},
        )
        self._start_dialog = StartSessionDialog(
            on_start=self._start_session, on_change=self._start_changed
        )
        self._start_dialog.show(self._form)
        self._start_dialog.present(self)

    def _start_changed(self, what: str, value: Any) -> None:
        if self._form is None:
            return
        if what == "profile":
            self.link.ask("profile.show", {"name": value}, self._start_profile_changed)
            return

        form = self._form
        self._form = start_model.start_form(
            profiles=list(form.profiles),
            profile=self._start_profile,
            duration_seconds=int(value) if what == "duration" else form.duration_seconds,
            level=str(value) if what == "level" else form.level,
            valve=(str(value) if what == "valve" else form.valve) or None,
            now=datetime.now().timestamp(),
            apps={app["id"]: app["name"] for app in self.installed},
        )
        if self._start_dialog is not None:
            self._start_dialog.show(self._form)

    def _start_profile_changed(self, reply: Reply) -> None:
        if not reply.ok or self._form is None:
            return
        self._start_profile = reply.result.get("profile", {})
        form = self._form
        self._form = start_model.start_form(
            profiles=list(form.profiles),
            profile=self._start_profile,
            duration_seconds=form.duration_seconds,
            level=form.level,
            valve=form.valve or None,
            now=datetime.now().timestamp(),
            apps={app["id"]: app["name"] for app in self.installed},
        )
        if self._start_dialog is not None:
            self._start_dialog.show(self._form)

    def _start_session(self, form: start_model.StartForm) -> None:
        type_, payload = start_model.request_for(form)
        self.link.ask(type_, payload, self._acted)

    # -- profiles, lists, settings ---------------------------------------

    def _choose_profile(self, name: str) -> None:
        self.profile_name = name
        self.link.ask("profile.show", {"name": name}, self._profile_loaded)

    def _profile_changed(self, what: str, value: Any) -> None:
        """One change on the Profiles screen, as a request the ratchet judges."""
        payload: dict[str, Any] = {"name": self.profile_name}

        match what:
            case "web_mode":
                payload["web_mode"] = value
            case "add_domain":
                payload["add_domains"] = [value]
            case "remove_domain":
                payload["remove_domains"] = [value]
            case "category":
                key, on = value
                payload["add_categories" if on else "remove_categories"] = [key]
            case "app":
                key, on = value
                payload["add_apps" if on else "remove_apps"] = [key]
            case "pattern":
                if value == profiles_model.CUSTOM:
                    return
                work, rest = value.split("/")
                payload["breaks"] = {"work_minutes": int(work), "break_minutes": int(rest)}
            case "break_type":
                payload["breaks"] = {"type": value}
            case "hardness":
                payload["breaks"] = {"hardness": value}
            case "allow_sites_during_breaks":
                payload["breaks"] = {"allow_sites_during_breaks": bool(value)}
            case _:
                return

        self.link.ask("profile.edit", payload, self._profile_edited)

    def _profile_edited(self, reply: Reply) -> None:
        if not reply.ok:
            self._complain(reply)
        self._load_profiles()

    def _remove_from_category(self, identifier: str, value: str) -> None:
        key = "remove_apps" if value.endswith(".desktop") else "remove_domains"
        self.link.ask("category.edit", {"id": identifier, key: [value]}, self._category_edited)

    def _category_edited(self, reply: Reply) -> None:
        if not reply.ok:
            self._complain(reply)
        self._load_lists()

    def _choose_range(self, which: str) -> None:
        self.stats_range = which
        self._load_stats()

    def _set_setting(self, key: str, value: Any) -> None:
        type_, payload = settings_model.set_request(key, value)
        self.link.ask(type_, payload, self._setting_changed)

    def _setting_changed(self, reply: Reply) -> None:
        if not reply.ok:
            self._complain(reply)
            self._load_settings()

    def _delete_statistics(self) -> None:
        dialog = Adw.AlertDialog(
            heading=_("Delete all statistics?"),
            body=_(
                "This cannot be undone. Anchor keeps no copy, because a private "
                "record that survives its own deletion is not private."
            ),
        )
        dialog.add_response("back", _("Cancel"))
        dialog.add_response("delete", _("Delete"))
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect(
            "response",
            lambda _d, response: (
                self.link.ask("stats.delete", {}, self._acted) if response == "delete" else None
            ),
        )
        dialog.present(self)

    def _edit_schedule(self, block: Any) -> None:
        if not block.editable:
            self._toast(block.reason)
            return
        self._toast(_("Editing schedules from here arrives with packaging."))

    # -- onboarding ------------------------------------------------------

    def show_onboarding(self) -> None:
        window = OnboardingWindow(
            parent=self,
            run_test=self._run_live_test,
            on_finished=self._onboarding_finished,
            indicator=_watcher(),
        )
        window.present()

    def _run_live_test(self, then: Any) -> None:
        """Block a sample domain for real, and check that it stopped resolving."""

        def after_setup(reply: Reply) -> None:
            if not reply.ok:
                then(
                    onboarding_model.TestResult(
                        ok=False,
                        failed=True,
                        title=_("The test could not start"),
                        detail=reply.message,
                        fix=_("Run `anchor doctor` to see what is wrong."),
                    )
                )
                return

            def look() -> bool:
                self._check_live_test(then)
                return False

            # A moment for the blocker to pick the policy up: it polls the
            # engine once a second, and the rules follow the poll.
            GLib.timeout_add_seconds(2, look)

        self.link.ask_all(onboarding_model.setup_requests(), after_setup)

    def _check_live_test(self, then: Any) -> None:
        sample = onboarding_model.resolves(onboarding_model.SAMPLE_DOMAIN)
        control = onboarding_model.resolves(onboarding_model.CONTROL_DOMAIN)
        then(onboarding_model.verdict_for(sample_resolves=sample, control_resolves=control))

        # The session ends by expiring; the profile goes once it has.
        def tidy_up() -> bool:
            self.link.ask_all(onboarding_model.cleanup_requests(), lambda _reply: None)
            return False

        GLib.timeout_add_seconds(onboarding_model.TEST_SECONDS + 2, tidy_up)

    def _onboarding_finished(self) -> None:
        type_, payload = onboarding_model.finished_request()
        self.link.ask(type_, payload, lambda _reply: None)

    # -- saying things ---------------------------------------------------

    def _complain(self, reply: Reply) -> None:
        """Show the engine's own words. A refusal is an answer (SPEC 7.4)."""
        self._toast(reply.message or _("Anchor could not do that."))

    def _toast(self, text: str) -> None:
        if text:
            self.toasts.add_toast(Adw.Toast(title=text, timeout=6))


def _watcher() -> bool | None:
    try:
        from anchor.agent.desktop import watcher_running

        return watcher_running()
    except ImportError:  # pragma: no cover - PyGObject is present if we got here
        return None


class AnchorApplication(Adw.Application):
    """``anchor-gui``."""

    def __init__(self, socket_path: Path) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        # What windows and the desktop call Anchor when they have to guess.
        GLib.set_application_name("Anchor")
        GLib.set_prgname(APP_ID)
        self.link = EngineLink(socket_path, schedule=lambda work: GLib.idle_add(_once(work)))
        self.window: AnchorWindow | None = None

    def do_activate(self) -> None:
        if self.window is None:
            install_styles(Gdk_display())
            self.window = AnchorWindow(self, self.link)
            self.link.start()
            self.window.refresh()
            self.link.ask("config.get", {"key": "onboarding_done"}, self._maybe_onboard)
        self.window.present()

    def _maybe_onboard(self, reply: Reply) -> None:
        """SPEC 14: the introduction runs on first run, and only then."""
        if not reply.ok or self.window is None:
            return
        settings = reply.result.get("settings", [])
        done = bool(settings and settings[0].get("value"))
        if not done:
            self.window.show_onboarding()

    def do_shutdown(self) -> None:
        self.link.stop()
        Adw.Application.do_shutdown(self)


def Gdk_display() -> Any:  # noqa: N802 - named for what it returns
    from gi.repository import Gdk

    return Gdk.Display.get_default()


def _once(work: Any) -> Any:
    def run() -> bool:
        work()
        return False

    return run
