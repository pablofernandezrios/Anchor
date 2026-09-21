"""The interface's one connection to the engine (SPEC 5.1).

The interface owns nothing and decides nothing. It asks the engine for a
status, draws it, sends what the user clicks, and draws the answer. This is
the only place in :mod:`anchor.gui` that touches a socket.

Three things it has to get right.

**Nothing blocks the drawing.** GTK runs one thread and a request that takes
five seconds on a busy machine would freeze the window for five seconds. Every
request is sent on a worker thread and its answer handed back through
``schedule``, which the GTK layer fills in with ``GLib.idle_add`` and tests
fill in with "call it now".

**The status arrives by itself.** :class:`~anchor.agent.feed.EngineFeed` is
reused rather than re-implemented: it reconnects for as long as it takes and
asks for the status on every reconnection, which is what an interface left
open across an engine restart needs.

**A refusal is an answer.** The engine refuses things on purpose — the
ratchet, a Strict cancellation, a setting a session has frozen — and those
refusals are the product working. They arrive here as an ordinary
:class:`Reply` with ``ok`` false and a sentence to show, not as an exception
to be caught somewhere far away.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self

from anchor.agent.feed import EngineFeed
from anchor.cli.client import EngineClient, EngineUnreachableError
from anchor.protocol.messages import Event, Response

log = logging.getLogger("anchor-gui")

#: How long a request may take before the interface gives up on it. Longer
#: than the command line's, because a person watching a spinner will wait, and
#: a statistics query on a big database is slow the first time.
TIMEOUT_SECONDS = 15.0

Then = Callable[["Reply"], None]
Scheduler = Callable[[Callable[[], None]], None]


@dataclass(frozen=True, slots=True)
class Reply:
    """What came back, in the shape the screens read it in."""

    ok: bool
    result: dict[str, Any] = field(default_factory=dict)
    code: str = ""
    message: str = ""

    @classmethod
    def of(cls, response: Response) -> Self:
        if response.ok:
            return cls(ok=True, result=dict(response.result))
        return cls(
            ok=False,
            code=response.error.get("code", ""),
            message=response.error.get("message", ""),
        )

    @classmethod
    def unreachable(cls, error: Exception) -> Self:
        """The engine is not there, which is not the same as a refusal."""
        return cls(ok=False, code="UNREACHABLE", message=str(error))

    @property
    def refused(self) -> bool:
        """Whether the engine answered and said no.

        Worth telling apart from an outage: a refusal is Anchor working as
        specified and belongs in front of the user as a sentence, while an
        outage is a broken machine and belongs in front of them as a banner.
        """
        return not self.ok and self.code != "UNREACHABLE"


class EngineLink:
    """Requests out, status and events in."""

    def __init__(
        self,
        socket_path: Path,
        *,
        schedule: Scheduler | None = None,
        timeout: float = TIMEOUT_SECONDS,
    ) -> None:
        self._path = socket_path
        self._schedule: Scheduler = schedule if schedule is not None else (lambda work: work())
        self._timeout = timeout

        self.status: dict[str, Any] = {}
        self.connected = False

        self._on_status: list[Callable[[dict[str, Any]], None]] = []
        self._on_event: list[Callable[[Event], None]] = []
        self._on_connection: list[Callable[[bool], None]] = []

        self.feed = EngineFeed(
            socket_path,
            on_status=self._took_status,
            on_event=self._took_event,
            on_connected=self._took_connection,
        )

    # -- listening -------------------------------------------------------

    def watch_status(self, listener: Callable[[dict[str, Any]], None]) -> None:
        self._on_status.append(listener)

    def watch_events(self, listener: Callable[[Event], None]) -> None:
        self._on_event.append(listener)

    def watch_connection(self, listener: Callable[[bool], None]) -> None:
        self._on_connection.append(listener)

    def _took_status(self, status: dict[str, Any]) -> None:
        self.status = dict(status)
        self._schedule(lambda: _tell(self._on_status, self.status))

    def _took_event(self, event: Event) -> None:
        self._schedule(lambda: _tell(self._on_event, event))

    def _took_connection(self, connected: bool) -> None:
        self.connected = connected
        self._schedule(lambda: _tell(self._on_connection, connected))

    # -- asking ----------------------------------------------------------

    def ask_now(self, type_: str, payload: dict[str, Any] | None = None) -> Reply:
        """Send one request and wait for it. Never call this while drawing."""
        try:
            with EngineClient(self._path, timeout=self._timeout) as client:
                return Reply.of(client.call(type_, payload or {}))
        except (EngineUnreachableError, OSError) as error:
            return Reply.unreachable(error)

    def ask(
        self, type_: str, payload: dict[str, Any] | None = None, then: Then | None = None
    ) -> None:
        """Send one request from a click, and answer on the drawing thread."""

        def work() -> None:
            reply = self.ask_now(type_, payload)
            if then is not None:
                self._schedule(lambda: _answer(then, reply, type_))

        threading.Thread(target=work, name=f"anchor-gui-{type_}", daemon=True).start()

    def ask_all(self, requests: list[tuple[str, dict[str, Any]]], then: Then) -> None:
        """Send several requests and answer once, with the results merged.

        Home needs the session, the schedules and today's figures to draw one
        screen. Three separate answers would draw it three times, each with
        two thirds of the numbers from a slightly different moment.
        """

        def work() -> None:
            merged: dict[str, Any] = {}
            failure: Reply | None = None
            try:
                with EngineClient(self._path, timeout=self._timeout) as client:
                    for type_, payload in requests:
                        reply = Reply.of(client.call(type_, payload))
                        if not reply.ok:
                            # One refusal does not spoil the rest: the screen
                            # shows what it has and says what it could not get.
                            merged[f"{type_}.error"] = reply.message
                            failure = failure or reply
                            continue
                        merged[type_] = reply.result
            except (EngineUnreachableError, OSError) as error:
                # Built here, not inside the lambda: Python unbinds `error`
                # when the except block ends, and the scheduler runs the
                # lambda later — on GTK's next idle, which is always after.
                outage = Reply.unreachable(error)
                self._schedule(lambda: _answer(then, outage, "ask_all"))
                return

            answer = Reply(ok=failure is None, result=merged)
            self._schedule(lambda: _answer(then, answer, "ask_all"))

        threading.Thread(target=work, name="anchor-gui-batch", daemon=True).start()

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        self.feed.start()

    def stop(self) -> None:
        self.feed.stop()

    def __enter__(self) -> EngineLink:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()


def _tell(listeners: list[Any], value: Any) -> None:
    for listener in list(listeners):
        try:
            listener(value)
        except Exception:
            # One screen that cannot draw must not stop the others, and must
            # certainly not end the feed they all read from.
            log.exception("a listener raised")


def _answer(then: Then, reply: Reply, what: str) -> None:
    try:
        then(reply)
    except Exception:
        log.exception("the handler for %s raised", what)
