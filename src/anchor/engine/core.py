"""The engine: the only component that decides anything (SPEC 5.1).

Every other part of Anchor is a thin client. The engine owns the configuration,
the state and the session timers, answers requests, and publishes events.

One reading of the specification is worth stating, because the wording leaves
room for two. SPEC 7.2 and 7.5 describe leaving as "cancel after a wait" and
"request unlock, wait 30 minutes", without a second confirmation step. Anchor
therefore treats a pending exit as taking effect by itself once its wait has
run out, unless it is withdrawn first, and asks for the phrase only where a
phrase is owed. The alternative reading, where the user must come back and
confirm, would quietly turn a 30-minute wait into an indefinite one for anyone
who walks away from the machine.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from anchor.engine.paths import Paths, Settings
from anchor.engine.profiles import Profile
from anchor.engine.ratchet import check_profile_change
from anchor.engine.refusal import apply_refusal, remove_refusal
from anchor.engine.sessions import (
    ExitKind,
    Rupture,
    Session,
    SessionPolicy,
    start_session,
)
from anchor.engine.state import EngineState
from anchor.engine.store import LoadStatus, SignedStore, load_or_create_key
from anchor.engine.timekeeping import Clock, SystemClock, reconcile
from anchor.protocol.errors import AnchorError, ErrorCode, RatchetViolationError
from anchor.protocol.messages import Event, Request, Response
from anchor.protocol.types import Level, RuptureKind, SessionOrigin, Valve, WebMode

log = logging.getLogger("anchord")

EventSink = Callable[[Event], None]


class Engine:
    """Holds the state and answers requests."""

    def __init__(
        self,
        paths: Paths,
        settings: Settings,
        *,
        clock: Clock | None = None,
        policy: SessionPolicy | None = None,
        systemd_runtime_dir: Path | None = None,
    ) -> None:
        self.paths = paths
        self.settings = settings
        self.systemd_runtime_dir = (
            systemd_runtime_dir if systemd_runtime_dir is not None else paths.systemd_runtime_dir
        )
        self.clock: Clock = clock or SystemClock()
        self.policy = policy or SessionPolicy()
        self.state = EngineState()
        self.profiles: dict[str, Profile] = {}
        self._sinks: list[EventSink] = []

        key = load_or_create_key(paths.key_file)
        self._config_store = SignedStore(paths.config_file, key)
        self._state_store = SignedStore(paths.state_file, key)

    # -- events ---------------------------------------------------------

    def subscribe(self, sink: EventSink) -> None:
        self._sinks.append(sink)

    def unsubscribe(self, sink: EventSink) -> None:
        if sink in self._sinks:
            self._sinks.remove(sink)

    def emit(self, event: str, payload: dict[str, Any] | None = None) -> None:
        message = Event(event=event, payload=payload or {})
        for sink in list(self._sinks):
            try:
                sink(message)
            except OSError:
                # A subscriber that went away must not take the engine with it.
                self.unsubscribe(sink)

    # -- persistence ----------------------------------------------------

    def load(self) -> None:
        """Read configuration and state from disk, then reconcile the clock."""
        self.paths.ensure_directories()

        config = self._config_store.load()
        if config.status is LoadStatus.TAMPERED:
            log.warning("config.json failed its integrity check: %s", config.detail)
        self.profiles = {
            name: Profile.from_dict(raw)
            for name, raw in (config.data.get("profiles") or {}).items()
        }

        stored = self._state_store.load()
        if stored.status is LoadStatus.TAMPERED:
            # Keeping the stricter interpretation means trusting the session
            # that was recorded rather than dropping it (SPEC 6.1, P4): an edit
            # to state.json must not be a way to end a session.
            log.warning("state.json failed its integrity check: %s", stored.detail)
            self.state = EngineState.from_dict(stored.data) if stored.data else EngineState()
            self._record_rupture(RuptureKind.TAMPERING, f"state.json was modified: {stored.detail}")
        else:
            self.state = EngineState.from_dict(stored.data)

        self.tick()
        self._match_refusal()

    def _match_refusal(self) -> None:
        """Keep the stop refusal in step with whether a session is running.

        Called on load as well as on the transitions, because /run is cleared
        by a reboot: a session that survived one needs its drop-ins written
        again, and a machine that crashed mid-session needs the stale ones
        removed (SPEC 5.3).
        """
        try:
            if self.state.session is not None:
                apply_refusal(runtime_dir=self.systemd_runtime_dir)
            else:
                remove_refusal(runtime_dir=self.systemd_runtime_dir)
        except OSError as error:
            # Friction, not a lock. A session that runs without it is still a
            # session; one that refuses to start because systemd would not
            # cooperate would be worse than the problem.
            log.warning("could not adjust the stop refusal: %s", error)

    def save(self) -> None:
        self._state_store.save(self.state.to_dict())

    def save_config(self) -> None:
        self._config_store.save(
            {"profiles": {name: profile.to_dict() for name, profile in self.profiles.items()}}
        )

    # -- the clock ------------------------------------------------------

    def tick(self) -> None:
        """Advance the session: expire it, or let a completed exit take effect.

        Called on a timer, and on boot and resume, where SPEC 6.2 requires
        expired sessions to end and active ones to resume with the time left.
        """
        session = self.state.session
        if session is None:
            return

        outcome = reconcile(session.anchor, self.clock)
        if outcome.anchor is not session.anchor:
            session = Session(**{**_as_kwargs(session), "anchor": outcome.anchor})
            self.state.session = session

        if outcome.tampered:
            self._record_rupture(
                RuptureKind.TAMPERING,
                f"the system clock moved by {outcome.wall_drift_seconds:.0f} seconds",
            )

        if outcome.expired:
            self._end_session(reason="completed")
            return

        pending = session.exit_request
        if (
            pending is not None
            and pending.phrase is None
            and pending.remaining_wait(self.clock) <= 0
        ):
            # The wait was the whole price, and it has been paid.
            result = session.complete_exit(clock=self.clock)
            if result.rupture is not None:
                self._store_rupture(result.rupture)
            self._end_session(reason=str(pending.kind))

    # -- requests -------------------------------------------------------

    def handle(self, request: Request) -> Response:
        handler = self._handlers().get(request.type)
        if handler is None:
            return request.fail(
                ErrorCode.NOT_IMPLEMENTED,
                f"{request.type} is not available in this build yet",
            )
        try:
            return handler(request)
        except AnchorError as error:
            return request.fail(error.code, error.message)
        except Exception:
            log.exception("unhandled error while serving %s", request.type)
            return request.fail(ErrorCode.INTERNAL, "the engine hit an unexpected error")

    def _handlers(self) -> dict[str, Callable[[Request], Response]]:
        return {
            "status.get": self._on_status,
            "session.start": self._on_start,
            "session.extend": self._on_extend,
            "session.cancel": self._on_cancel,
            "session.withdraw_cancel": self._on_withdraw,
            "valve.request": self._on_valve_request,
            "valve.withdraw": self._on_withdraw,
            "valve.phrase": self._on_valve_phrase,
            "profile.list": self._on_profile_list,
            "profile.show": self._on_profile_show,
            "profile.create": self._on_profile_create,
            "profile.edit": self._on_profile_edit,
            "profile.delete": self._on_profile_delete,
            "policy.get": self._on_policy,
            "blocked.report": self._on_blocked_report,
            "tamper.report": self._on_tamper_report,
        }

    # -- profiles ---------------------------------------------------------

    def _require_profile(self, name: str) -> Profile:
        profile = self.profiles.get(name)
        if profile is None:
            raise AnchorError(
                f"there is no profile called {name!r}", code=ErrorCode.UNKNOWN_PROFILE
            )
        return profile

    def _profile_in_use(self, name: str) -> bool:
        """Whether a running session is enforcing this profile.

        The ratchet protects the session that is running, not the whole
        configuration file. A profile nothing is using can be edited freely,
        because there is no way to switch a session onto it mid-flight.
        """
        self.tick()
        return self.state.session is not None and self.state.session.profile == name

    def _on_profile_list(self, request: Request) -> Response:
        return request.ok({"profiles": sorted(self.profiles)})

    def _on_profile_show(self, request: Request) -> Response:
        profile = self._require_profile(str(request.payload["name"]))
        return request.ok({"profile": profile.to_dict()})

    def _on_profile_create(self, request: Request) -> Response:
        name = str(request.payload["name"])
        if name in self.profiles:
            raise AnchorError(
                f"a profile called {name!r} already exists", code=ErrorCode.INVALID_CONFIG
            )

        payload = request.payload
        try:
            profile = Profile(
                name=name,
                web_mode=WebMode(payload["web_mode"])
                if payload.get("web_mode")
                else WebMode.BLOCKLIST,
                domains=frozenset(payload.get("domains") or ()),
                apps=frozenset(payload.get("apps") or ()),
                categories=frozenset(payload.get("categories") or ()),
            )
        except ValueError as error:
            raise AnchorError(str(error), code=ErrorCode.INVALID_CONFIG) from error

        self.profiles[name] = profile
        self.save_config()
        self.emit("profile.changed", {"name": name})
        return request.ok({"profile": profile.to_dict()})

    def _on_profile_edit(self, request: Request) -> Response:
        payload = request.payload
        name = str(payload["name"])
        current = self._require_profile(name)

        def changed(base: frozenset[str], added: str, removed: str) -> frozenset[str]:
            """Apply one field's additions and removals.

            The result is what the ratchet judges: the caller says what to add
            and what to take away, and taking away is what a session refuses.
            """
            plus: list[str] = list(payload.get(added) or ())
            minus: list[str] = list(payload.get(removed) or ())
            return (base | frozenset(plus)) - frozenset(minus)

        proposed = replace(
            current,
            web_mode=(
                WebMode(payload["web_mode"]) if payload.get("web_mode") else current.web_mode
            ),
            domains=changed(current.domains, "add_domains", "remove_domains"),
            apps=changed(current.apps, "add_apps", "remove_apps"),
            categories=changed(current.categories, "add_categories", "remove_categories"),
            block_vpn_and_tor=(
                current.block_vpn_and_tor
                if payload.get("block_vpn_and_tor") is None
                else bool(payload["block_vpn_and_tor"])
            ),
        )

        # Checked before anything is written, so a refused edit leaves the
        # profile exactly as it was rather than half applied.
        if self._profile_in_use(name):
            check_profile_change(current, proposed)

        self.profiles[name] = proposed
        self.save_config()
        self.emit("profile.changed", {"name": name})
        log.info("profile %s edited", name)
        return request.ok({"profile": proposed.to_dict()})

    def _on_profile_delete(self, request: Request) -> Response:
        name = str(request.payload["name"])
        self._require_profile(name)

        if self._profile_in_use(name):
            raise RatchetViolationError(f"cannot delete {name!r} while a session is enforcing it")

        del self.profiles[name]
        self.save_config()
        self.emit("profile.changed", {"name": name})
        return request.ok({"deleted": name})

    def _on_policy(self, request: Request) -> Response:
        """Tell the blocker what to block (SPEC 5.1).

        Only the user's own rules. The essentials list and the DoH endpoints
        are the blocker's business: they are how blocking is made to work
        rather than choices the user made, and the engine has no opinion on
        them.
        """
        self.tick()
        session = self.state.session
        if session is None:
            return request.ok({"active": False})

        profile = self.profiles.get(session.profile)
        if profile is None:
            # A session naming a profile that no longer exists still blocks.
            # Dropping to "nothing blocked" would turn a missing profile into
            # a way out (P4).
            log.warning(
                "session %s names profile %r, which is not in the configuration; "
                "blocking everything until it is restored",
                session.id,
                session.profile,
            )
            return request.ok(
                {
                    "active": True,
                    "mode": str(WebMode.ALLOWLIST),
                    "domains": [],
                    # A profile that vanished must not become a way to keep a
                    # tunnel up either.
                    "block_tunnels": session.level is Level.STRICT,
                }
            )

        return request.ok(
            {
                "active": True,
                "mode": str(profile.web_mode),
                "domains": sorted(profile.domains),
                # Two conditions, and the engine owns both. SPEC 7.2 allows VPN
                # and Tor below Strict, and ADR 4 lets a profile opt out of
                # blocking them even in Strict.
                "block_tunnels": session.level is Level.STRICT and profile.block_vpn_and_tor,
            }
        )

    def _on_blocked_report(self, request: Request) -> Response:
        """Record an attempt the blocker refused (SPEC 8.3, 13)."""
        session = self.state.session
        if session is None:
            # The session ended between the block and the report. Nothing to
            # attribute it to, and not worth an error.
            return request.ok({"recorded": False})

        self.state.session = session.with_blocked_attempt()
        self.save()

        domain = str(request.payload["domain"])
        rule = str(request.payload["rule"])
        # This event carries a domain, and goes only to subscribers in the
        # user's own session so the agent can show a notification. It is never
        # written to the log (SPEC 13).
        self.emit("blocked.attempt", {"domain": domain, "rule": rule})
        return request.ok({"recorded": True, "total": self.state.session.blocked_attempts})

    def _on_tamper_report(self, request: Request) -> Response:
        """Record manipulation the blocker noticed (SPEC 7.6).

        The blocker puts the rules back itself, because it is the one holding
        them and waiting for an instruction would leave the machine unblocked
        in the meantime. What it cannot do is decide what the event means, so
        it reports, and the engine writes the rupture.
        """
        kind = str(request.payload["kind"])
        detail = str(request.payload["detail"])

        self._record_rupture(RuptureKind.TAMPERING, f"{kind}: {detail}")
        return request.ok({"recorded": True})

    def _on_status(self, request: Request) -> Response:
        self.tick()
        return request.ok(self.status())

    def status(self) -> dict[str, Any]:
        """The snapshot every client renders (SPEC 14, 15)."""
        session = self.state.session
        if session is None:
            return {
                "active": False,
                "skips_remaining": self.state.skips_remaining,
                "profiles": sorted(self.profiles),
            }

        outcome = reconcile(session.anchor, self.clock)
        pending = session.exit_request
        return {
            "active": True,
            "session_id": session.id,
            "profile": session.profile,
            "level": str(session.level),
            "origin": str(session.origin),
            "valve": str(session.valve) if session.valve else None,
            "phase": str(session.phase),
            "started_at": session.anchor.started_at,
            "ends_at": session.anchor.ends_at,
            "remaining_seconds": outcome.remaining_seconds,
            "blocked_attempts": session.blocked_attempts,
            "skips_remaining": self.state.skips_remaining,
            "exit_request": (
                {
                    "kind": str(pending.kind),
                    "remaining_wait_seconds": pending.remaining_wait(self.clock),
                    "phrase": pending.phrase,
                }
                if pending
                else None
            ),
        }

    def _on_start(self, request: Request) -> Response:
        if self.state.session is not None:
            raise AnchorError("a session is already running", code=ErrorCode.SESSION_ALREADY_ACTIVE)

        payload = request.payload
        profile_name = str(payload["profile"])
        if self.profiles and profile_name not in self.profiles:
            raise AnchorError(
                f"there is no profile called {profile_name!r}",
                code=ErrorCode.UNKNOWN_PROFILE,
            )

        level: Level = payload["level"]
        valve: Valve | None = payload["valve"]
        origin: SessionOrigin = payload["origin"]

        session = start_session(
            profile=profile_name,
            level=level,
            duration_seconds=float(payload["duration_seconds"]),
            valve=valve,
            origin=origin,
            clock=self.clock,
            policy=self.policy,
        )
        self.state.session = session
        self.save()
        self._match_refusal()

        self.emit("session.started", self.status())
        log.info(
            "session %s started: profile=%s level=%s duration=%.0fs",
            session.id,
            session.profile,
            session.level,
            session.anchor.duration_seconds,
        )
        return request.ok(self.status())

    def _on_extend(self, request: Request) -> Response:
        session = self._require_session()
        self.state.session = session.extended_by(float(request.payload["by_seconds"]))
        self.save()
        self.emit("session.extended", self.status())
        return request.ok(self.status())

    def _on_cancel(self, request: Request) -> Response:
        session = self._require_session()
        typed = request.payload.get("typed")

        if session.exit_request is None:
            self.state.session = session.request_exit(
                ExitKind.CANCEL, clock=self.clock, policy=self.policy
            )
            self.save()
            return request.ok(self.status())

        return self._try_complete(request, session, typed)

    def _on_valve_request(self, request: Request) -> Response:
        session = self._require_session()
        if session.exit_request is not None:
            return request.ok(self.status())

        self.state.session = session.request_exit(
            ExitKind.VALVE, clock=self.clock, policy=self.policy
        )
        self.save()
        self.emit("valve.requested", self.status())
        return request.ok(self.status())

    def _on_valve_phrase(self, request: Request) -> Response:
        session = self._require_session()
        return self._try_complete(request, session, str(request.payload["text"]))

    def _on_withdraw(self, request: Request) -> Response:
        session = self._require_session()
        if session.exit_request is None:
            raise AnchorError("there is nothing to withdraw", code=ErrorCode.VALVE_NOT_REQUESTED)
        self.state.session = session.withdraw_exit()
        self.save()
        self.emit("valve.withdrawn", self.status())
        return request.ok(self.status())

    def _try_complete(self, request: Request, session: Session, typed: str | None) -> Response:
        pending = session.exit_request
        outcome = session.complete_exit(clock=self.clock, typed=typed)
        if outcome.rupture is not None:
            self._store_rupture(outcome.rupture)
        self._end_session(reason=str(pending.kind) if pending else "cancel")
        return request.ok({"ended": outcome.ended, **self.status()})

    # -- helpers --------------------------------------------------------

    def _require_session(self) -> Session:
        self.tick()
        session = self.state.session
        if session is None:
            raise AnchorError("no session is running", code=ErrorCode.NO_ACTIVE_SESSION)
        return session

    def _end_session(self, *, reason: str) -> None:
        session = self.state.session
        if session is None:
            return
        self.state.session = None
        self.save()
        self._match_refusal()
        log.info("session %s ended: %s", session.id, reason)
        self.emit(
            "session.ended",
            {"session_id": session.id, "profile": session.profile, "reason": reason},
        )

    def _record_rupture(self, kind: RuptureKind, detail: str) -> None:
        self._store_rupture(Rupture(kind=kind, at=self.clock.wall(), detail=detail))

    def _store_rupture(self, rupture: Rupture) -> None:
        self.state.ruptures.append(rupture.to_dict())
        self.save()
        log.warning("rupture recorded: %s (%s)", rupture.kind, rupture.detail)
        self.emit("rupture.recorded", rupture.to_dict())


def _as_kwargs(session: Session) -> dict[str, Any]:
    return {
        "id": session.id,
        "profile": session.profile,
        "level": session.level,
        "origin": session.origin,
        "anchor": session.anchor,
        "valve": session.valve,
        "phase": session.phase,
        "exit_request": session.exit_request,
        "blocked_attempts": session.blocked_attempts,
        "ruptures": session.ruptures,
    }
