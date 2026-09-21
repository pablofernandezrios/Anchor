"""``anchor-agent``: the user's half of Anchor (SPEC 5.1).

It owns nothing. It follows the engine, draws the indicator SPEC 14.1
describes, and shows the notifications SPEC 7.1, 8.3 and 9 ask for. Every
decision it makes about what to say lives in :mod:`indicator` and
:mod:`notifications`, where it can be tested; this file is the wiring.

Two things it deliberately does not do. It never decides that a session ended:
only the engine does that, and an agent that timed out and drew its own
conclusion would eventually be wrong in the direction that matters. And it
never blocks anything: a user process cannot be trusted to, and P4 says the
blocks must not depend on it.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from collections.abc import Callable
from types import FrameType
from typing import Any

from anchor.agent.breakscreen import screen_for
from anchor.agent.feed import EngineFeed
from anchor.agent.indicator import IndicatorModel
from anchor.agent.notifications import Notification, plan
from anchor.cli.client import EngineClient, EngineUnreachableError
from anchor.engine.paths import Paths
from anchor.protocol.messages import Event

log = logging.getLogger("anchor-agent")


class Agent:
    """The engine's events on one side, the desktop on the other."""

    def __init__(
        self,
        paths: Paths,
        *,
        tray: Any = None,
        notifier: Any = None,
        overlay: Any = None,
        schedule: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        self.paths = paths
        self.model = IndicatorModel()
        self._tray = tray
        self._notifier = notifier
        self._overlay = overlay
        # Drawing happens on the desktop's own thread when there is one; the
        # feed runs on its own, and D-Bus is not the place to find out what
        # happens when two threads meet.
        self._schedule = schedule if schedule is not None else (lambda work: work())

        self.feed = EngineFeed(
            paths.engine_socket,
            on_status=self.on_status,
            on_event=self.on_event,
            on_connected=self.on_connected,
        )

    # -- what the engine says --------------------------------------------

    def on_status(self, status: dict[str, Any]) -> None:
        self.model.update_status(status)
        self._draw()
        self._draw_break(status)

    def on_connected(self, connected: bool) -> None:
        self.model.note_connection(connected)
        self._draw()

    def on_event(self, event: Event) -> None:
        self.model.note_event(event.event, event.payload)
        self._draw()
        if event.event.startswith("break."):
            # The next tick would do it a second later, which is a second of
            # overlay after a break the user just skipped.
            self._draw_break(self.model.status)

        notification = plan(event.event, event.payload)
        if notification is not None:
            self._schedule(lambda: self._notify(notification))

    # -- what the desktop shows ------------------------------------------

    def _draw(self) -> None:
        view = self.model.view
        if self._tray is None:
            log.debug("indicator: %s", view)
            return
        self._schedule(lambda: self._show(view))

    def _show(self, view: Any) -> None:
        try:
            self._tray.show(view)
        except Exception:
            # The panel is not worth the session. Anchor keeps blocking with
            # or without a top bar.
            log.exception("could not update the indicator")

    def use_overlay(self, overlay: Any) -> None:
        """Attach the break overlay.

        Set after construction rather than passed in, because the overlay's
        buttons talk back to the agent and one of the two has to exist first.
        """
        self._overlay = overlay

    def _draw_break(self, status: dict[str, Any]) -> None:
        """Put the overlay up, take it down, or leave it alone (SPEC 10)."""
        if self._overlay is None:
            return
        screen = screen_for(status)
        self._schedule(lambda: self._show_break(screen))

    def _show_break(self, screen: Any) -> None:
        try:
            if screen is None:
                if self._overlay.showing:
                    self._overlay.hide()
                return
            self._overlay.show(screen)
        except Exception:
            # A break the user cannot see is a worse break, not a broken
            # session. The engine keeps counting either way.
            log.exception("could not draw the break overlay")

    def ask_engine(self, request: str) -> None:
        """Send one request, from a button on the overlay (SPEC 10).

        On a thread, because it happens on a click and a socket that takes
        five seconds to answer would freeze the screen it was clicked on.
        """

        def send() -> None:
            try:
                with EngineClient(self.paths.engine_socket, timeout=5.0) as client:
                    response = client.call(request)
                if not response.ok:
                    log.warning("the engine refused %s: %s", request, response.error.get("message"))
            except (EngineUnreachableError, OSError) as error:
                log.warning("could not send %s: %s", request, error)

        threading.Thread(target=send, name="anchor-request", daemon=True).start()

    def _notify(self, notification: Notification) -> None:
        if self._notifier is None:
            log.info("notification: %s — %s", notification.summary, notification.body)
            return
        try:
            self._notifier.send(notification)
        except Exception:
            log.exception("could not show a notification")

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        self.feed.start()

    def stop(self) -> None:
        self.feed.stop()


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="anchor-agent", description=__doc__)
    parser.add_argument("--root", help="Follow an engine on a relocated tree. Development only.")
    parser.add_argument("--verbose", action="store_true", help="Log at debug level.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Follow the engine and log what would be shown, touching no desktop.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    paths = Paths.resolve(args.root)

    if args.dry_run:
        return _run_headless(paths)
    return _run_on_the_desktop(paths)


def _run_headless(paths: Paths) -> int:
    """Everything except the drawing, for a machine with no session bus."""
    agent = Agent(paths)
    stopping = threading.Event()

    def stop(signum: int, _frame: FrameType | None) -> None:
        log.info("received signal %d, shutting down", signum)
        stopping.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    agent.start()
    try:
        stopping.wait()
    finally:
        agent.stop()
    return 0


def _run_on_the_desktop(paths: Paths) -> int:
    from anchor.agent.desktop import (
        DesktopNotifier,
        DesktopUnavailableError,
        TrayItem,
        session_bus,
    )

    try:
        connection = session_bus()
        tray = TrayItem(connection)
        notifier = DesktopNotifier(connection)
    except DesktopUnavailableError as error:
        # Refusing to start would take the notifications down with the panel,
        # and systemd would restart us into the same wall every two seconds.
        log.warning("%s", error)
        log.warning("running without a desktop; use --dry-run to silence this")
        return _run_headless(paths)

    from gi.repository import GLib

    loop = GLib.MainLoop()
    agent = Agent(
        paths,
        tray=tray,
        notifier=notifier,
        schedule=lambda work: GLib.idle_add(_once(work)),
    )
    agent.use_overlay(_make_overlay(agent))

    tray.start(on_activate=_open_the_interface)
    agent.start()

    for received in (signal.SIGTERM, signal.SIGINT):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, received, _quit(loop))

    try:
        loop.run()
    finally:
        agent.stop()
        tray.stop()
    return 0


def _make_overlay(agent: Agent) -> Any:
    """The break overlay, if this desktop can carry one (SPEC 10, ADR 2)."""
    from anchor.agent.overlay import BreakOverlay, OverlayUnavailableError

    try:
        return BreakOverlay(
            on_postpone=lambda: agent.ask_engine("break.postpone"),
            on_skip=lambda: agent.ask_engine("break.skip"),
        )
    except OverlayUnavailableError as error:
        # The notification still arrives, which is the part ADR 2 promises.
        log.warning("no break overlay: %s", error)
        return None


def _once(work: Callable[[], None]) -> Callable[[], bool]:
    """Wrap work for ``idle_add``, which repeats anything that returns True."""

    def run() -> bool:
        work()
        return False

    return run


def _quit(loop: Any) -> Callable[[], bool]:
    def stop() -> bool:
        log.info("shutting down")
        loop.quit()
        return False

    return stop


def _open_the_interface() -> None:
    """Clicking the indicator opens Anchor (SPEC 14.1).

    There is no interface to open yet; it arrives with Milestone 8. Saying so
    in the log beats a click that silently does nothing.
    """
    log.info("the indicator was clicked; the interface arrives in Milestone 8")


if __name__ == "__main__":
    sys.exit(main())
