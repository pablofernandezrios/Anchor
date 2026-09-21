#!/usr/bin/env python3
"""Run the interface against a real engine and photograph every screen.

    xvfb-run -a python3 tools/screenshot_gui.py --out /tmp/anchor-shots

This is not a mock. It starts a real :class:`~anchor.engine.core.Engine` on a
temporary tree, seeds it with the data the approved mockups show, serves it on
a real socket, and points a real ``anchor-gui`` at it. Every page is then
drawn by GTK and saved as a PNG.

Two things it is for. It is how the interface was checked against the mockups
while it was being written, on a machine with no desktop at all — GTK renders
perfectly well onto a virtual display, and a screen that crashes on being
drawn crashes here too. And it is what a reviewer runs to see the whole
interface in eight pictures without installing Anchor.

It proves rendering, not behaviour: nothing here clicks anything, and the
desktop check in ``tools/check_gui.py`` is still where a person confirms that
the real thing works on a real session.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from anchor.engine.core import Engine  # noqa: E402
from anchor.engine.paths import Paths, Settings  # noqa: E402
from anchor.engine.profiles import BreakSettings, Profile  # noqa: E402
from anchor.engine.schedules import Schedule  # noqa: E402
from anchor.engine.service import EngineServer  # noqa: E402
from anchor.protocol.types import BreakHardness, Level, WebMode  # noqa: E402

PAGES = ("home", "profiles", "schedules", "lists", "stats", "settings")


def seed(paths: Paths) -> Engine:
    """An engine holding what the mockups draw."""
    paths.ensure_directories()
    paths.shipped_categories.mkdir(parents=True, exist_ok=True)
    for name, title, domains, apps in (
        ("social", "Social media", ["x.com", "facebook.com", "reddit.com"], ["discord.desktop"]),
        ("video", "Video", ["youtube.com", "twitch.tv"], []),
        ("news", "News", ["news.ycombinator.com"], []),
        ("games", "Games", ["steampowered.com"], ["steam.desktop"]),
        ("shopping", "Shopping", ["amazon.com"], []),
    ):
        listed = "".join(f'  "{one}",\n' for one in domains)
        bundled = "".join(f'  "{one}",\n' for one in apps)
        (paths.shipped_categories / f"{name}.toml").write_text(
            f'name = "{title}"\ndomains = [\n{listed}]\napps = [\n{bundled}]\n',
            encoding="utf-8",
        )

    engine = Engine(paths, Settings(owner_uid=os.getuid()))
    engine.load()

    engine.profiles["Study"] = Profile(
        name="Study",
        web_mode=WebMode.BLOCKLIST,
        categories=frozenset({"social", "video", "news", "games"}),
        domains=frozenset({"twitch.tv", "news.ycombinator.com"}),
        apps=frozenset({"discord.desktop", "steam.desktop"}),
        breaks=BreakSettings(work_minutes=50, break_minutes=10, hardness=BreakHardness.MODERATE),
    )
    engine.profiles["Work"] = Profile(name="Work", categories=frozenset({"social"}))

    for name, profile, days, start, end, level in (
        ("Study", "Study", {0, 2, 4}, 9 * 60, 13 * 60, Level.FIRM),
        ("Work", "Work", {0, 1, 2, 3, 4}, 16 * 60, 19 * 60, Level.STRICT),
        ("Reading", "Study", {5, 6}, 10 * 60, 12 * 60, Level.SOFT),
    ):
        schedule = Schedule.create(
            name=name,
            profile=profile,
            days=frozenset(days),
            start_minute=start,
            end_minute=end,
            level=level,
            valve="wait" if level is Level.STRICT else None,  # type: ignore[arg-type]
        )
        engine.schedules[schedule.id] = schedule

    engine.save_config()
    _statistics(engine)
    return engine


def _statistics(engine: Engine) -> None:
    """A fortnight of plausible history, so the charts have something to draw."""
    today = datetime.now().date()
    for back in range(13, -1, -1):
        day = today - timedelta(days=back)
        session = f"seed-{back}"
        engine.stats.session_started(
            session_id=session,
            profile="Study",
            level="firm",
            origin="manual",
            at=datetime(day.year, day.month, day.day, 9).timestamp(),
        )
        hours = 2 + back % 4
        engine.stats.session_ended(
            session_id=session,
            started_at=datetime(day.year, day.month, day.day, 9).timestamp(),
            ended_at=datetime(day.year, day.month, day.day, 9 + hours, 30).timestamp(),
            reason="completed",
        )
        for target, count in (("youtube.com", 3 + back % 5), ("reddit.com", 1 + back % 3)):
            for _ in range(count):
                engine.stats.attempt(
                    session_id=session,
                    kind="domain",
                    target=target,
                    at=datetime(day.year, day.month, day.day, 10).timestamp(),
                )
        engine.stats.attempt(
            session_id=session,
            kind="app",
            target="discord.desktop",
            at=datetime(day.year, day.month, day.day, 11).timestamp(),
        )
        for outcome, many in (("taken", 2), ("postponed", back % 2)):
            for _ in range(many):
                engine.stats.break_outcome(
                    session_id=session,
                    outcome=outcome,
                    at=datetime(day.year, day.month, day.day, 12).timestamp(),
                )


def start_session(engine: Engine) -> None:
    """A session running, because that is what mockup 1 draws."""
    from anchor.protocol.messages import Request, new_id

    engine.handle(
        Request.from_dict(
            {
                "v": 1,
                "id": new_id(),
                "type": "session.start",
                "payload": {
                    "profile": "Study",
                    "duration_seconds": 8077,
                    "level": "firm",
                },
            }
        )
    )
    engine.state.session = engine.state.session.with_blocked_attempt()  # type: ignore[union-attr]
    engine.save()


def photograph(socket_path: Path, out: Path, *, dark: bool = False) -> int:
    """Draw every page and save it."""
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, GLib, Gtk

    from anchor.gui.app import AnchorApplication

    out.mkdir(parents=True, exist_ok=True)
    problems: list[str] = []

    application = AnchorApplication(socket_path)

    def capture(widget: Any, path: Path) -> None:
        width = widget.get_width()
        height = widget.get_height()
        if not width or not height:
            problems.append(f"{path.name}: the window had no size")
            return
        paintable = Gtk.WidgetPaintable.new(widget)
        snapshot = Gtk.Snapshot.new()
        paintable.snapshot(snapshot, width, height)
        node = snapshot.to_node()
        if node is None:
            problems.append(f"{path.name}: nothing was drawn")
            return
        widget.get_native().get_renderer().render_texture(node, None).save_to_png(str(path))
        print(f"  {path.name}")

    def run() -> bool:
        window = application.window
        assert window is not None
        for page in PAGES:
            window.stack.set_visible_child_name(page)
            _settle()
            capture(window, out / f"{page}.png")

        window.stack.set_visible_child_name("home")
        _settle()
        window.show_start_session()
        _settle()
        dialog = _dialog(window)
        if dialog is not None:
            capture(dialog, out / "start-session.png")
            dialog.close()
        else:
            problems.append("start-session.png: the dialog never appeared")

        _settle()
        window.show_onboarding()
        _settle()
        introduction = _window(application, "OnboardingWindow")
        if introduction is not None:
            capture(introduction, out / "onboarding.png")
            introduction.close()
        else:
            problems.append("onboarding.png: the window never appeared")

        application.quit()
        return False

    def _settle(rounds: int = 60) -> None:
        """Let GTK finish laying out, and the engine's answers arrive."""
        deadline = time.monotonic() + 3.0
        for _ in range(rounds):
            while GLib.MainContext.default().pending():
                GLib.MainContext.default().iteration(False)
            time.sleep(0.03)
            if time.monotonic() > deadline:
                break

    def _dialog(window: Any) -> Any:
        for child in _descendants(window):
            if isinstance(child, Adw.Dialog):
                return child
        return None

    def _window(app: Any, kind: str) -> Any:
        for one in app.get_windows():
            if type(one).__name__ == kind:
                return one
        return None

    def _descendants(widget: Any) -> list[Any]:
        found: list[Any] = []
        child = widget.get_first_child()
        while child is not None:
            found.append(child)
            found.extend(_descendants(child))
            child = child.get_next_sibling()
        return found

    if dark:
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)

    GLib.timeout_add(1500, run)
    application.run([])

    for problem in problems:
        print(f"PROBLEM {problem}", file=sys.stderr)
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="screenshot_gui.py", description=__doc__)
    parser.add_argument("--out", default="/tmp/anchor-shots", help="Where to put the pictures.")
    parser.add_argument("--dark", action="store_true", help="Photograph the dark theme.")
    parser.add_argument("--keep", action="store_true", help="Keep the temporary engine tree.")
    args = parser.parse_args(argv)

    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        print(
            "No display. Run this under xvfb-run, or on a desktop:\n"
            "    xvfb-run -a python3 tools/screenshot_gui.py",
            file=sys.stderr,
        )
        return 2

    tree = Path(tempfile.mkdtemp(prefix="anchor-shots-"))
    paths = Paths.resolve(tree)
    engine = seed(paths)
    start_session(engine)

    server = EngineServer(engine)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        return photograph(paths.engine_socket, Path(args.out), dark=args.dark)
    finally:
        server.shutdown()
        thread.join(timeout=5)
        if not args.keep:
            shutil.rmtree(tree, ignore_errors=True)


if __name__ == "__main__":
    # Convenience: re-run ourselves under a virtual display rather than making
    # every caller remember to.
    _headless = not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY")
    if os.environ.get("ANCHOR_SHOTS_REEXEC") != "1" and _headless and shutil.which("xvfb-run"):
        os.environ["ANCHOR_SHOTS_REEXEC"] = "1"
        raise SystemExit(
            subprocess.call(["xvfb-run", "-a", sys.executable, __file__, *sys.argv[1:]])
        )
    raise SystemExit(main())
