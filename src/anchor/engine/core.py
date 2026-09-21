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
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from anchor.engine.breaks import BreakState
from anchor.engine.breaks import advance as advance_breaks
from anchor.engine.breaks import postpone as postpone_break
from anchor.engine.breaks import skip as skip_break
from anchor.engine.categories import Category, load_categories, resolve
from anchor.engine.doctor import Observer, examine, gather, worst
from anchor.engine.paths import Paths, Settings
from anchor.engine.preferences import PREFERENCES, Preferences, describe
from anchor.engine.profiles import Profile
from anchor.engine.ratchet import check_profile_change
from anchor.engine.refusal import apply_refusal, remove_refusal
from anchor.engine.schedules import (
    Merged,
    Schedule,
    active_at,
    merge,
    parse_clock,
    parse_day,
)
from anchor.engine.sessions import (
    GRACE_SECONDS,
    ExitKind,
    Rupture,
    Session,
    SessionPolicy,
    start_session,
)
from anchor.engine.state import SKIPS_PER_WEEK, EngineState
from anchor.engine.stats import Statistics, summarise
from anchor.engine.store import LoadStatus, SignedStore, load_or_create_key
from anchor.engine.timekeeping import Clock, SystemClock, reconcile
from anchor.protocol.errors import AnchorError, ErrorCode, RatchetViolationError
from anchor.protocol.messages import Event, Request, Response
from anchor.protocol.types import (
    Level,
    RuptureKind,
    SessionOrigin,
    SessionPhase,
    Valve,
    WebMode,
)

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
        # The policy Anchor ships with, and the policy after the user's
        # settings are applied over it. Keeping both means a preference that
        # is set and then unset goes back to the default rather than to
        # whatever it happened to be before (SPEC 7.2).
        self._base_policy = policy or SessionPolicy()
        self.policy = self._base_policy
        self.preferences = Preferences()
        self.state = EngineState()
        self.profiles: dict[str, Profile] = {}
        self.categories: dict[str, Category] = {}
        self.schedules: dict[str, Schedule] = {}
        self._sinks: list[EventSink] = []
        # When anchor-blockerd last asked what to enforce. It is the only way
        # the engine hears from it at all, which makes it the only liveness
        # signal there is without asking systemd (SPEC 15).
        self._blocker_seen: float | None = None

        self.stats = Statistics(paths.stats_db, retention_days=settings.retention_days)

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

        self.categories = load_categories(self.paths.shipped_categories, self.paths.user_categories)

        config = self._config_store.load()
        if config.status is LoadStatus.TAMPERED:
            log.warning("config.json failed its integrity check: %s", config.detail)
        self.profiles = {
            name: Profile.from_dict(raw)
            for name, raw in (config.data.get("profiles") or {}).items()
        }
        self.preferences = Preferences.from_dict(config.data.get("preferences") or {})
        self._apply_preferences()
        self.schedules = {}
        for raw_schedule in config.data.get("schedules") or []:
            try:
                loaded = Schedule.from_dict(raw_schedule)
            except (KeyError, ValueError, AnchorError) as error:
                # One unreadable schedule must not cost the others, and must
                # certainly not stop the engine from starting.
                log.warning("a schedule could not be read and was skipped: %s", error)
                continue
            self.schedules[loaded.id] = loaded

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
            {
                "profiles": {name: profile.to_dict() for name, profile in self.profiles.items()},
                "schedules": [
                    schedule.to_dict() for schedule in _by_start(self.schedules.values())
                ],
                "preferences": self.preferences.to_dict(),
            }
        )

    def _apply_preferences(self) -> None:
        """Let the settings reach the parts of the engine they govern."""
        self.policy = self.preferences.policy(self._base_policy)
        self.stats.retention_days = self.preferences.retention(
            installed=self.settings.retention_days
        )

    # -- the clock ------------------------------------------------------

    def tick(self) -> None:
        """Advance the session: expire it, or let a completed exit take effect.

        Called on a timer, and on boot and resume, where SPEC 6.2 requires
        expired sessions to end and active ones to resume with the time left.
        """
        session = self.state.session
        if session is None:
            self._start_scheduled_session()
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
            self._start_scheduled_session()
            return

        self._advance_breaks(session)
        session = self.state.session
        if session is None:  # pragma: no cover - a break cannot end a session
            return

        if self._scheduled_window_closed(session):
            self._end_session(reason="schedule_ended")
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

    # -- schedules -------------------------------------------------------

    def current_occurrences(self) -> list[Any]:
        """The schedules covering this instant, minus the ones skipped."""
        now = self.clock.wall()
        self._forget_old_skips(now)
        return [
            occurrence
            for occurrence in active_at(list(self.schedules.values()), now)
            if self.state.skipped.get(occurrence.id) != occurrence.ends_at
        ]

    def merged_now(self) -> Merged | None:
        return merge(self.current_occurrences(), self.profiles)

    def _start_scheduled_session(self) -> None:
        """Start the session a schedule asks for (SPEC 11).

        A late boot joins the window in progress: the session starts now and
        ends when the window does. Nothing is owed for the part that was
        missed — a schedule is a promise about a time of day, not a quota of
        hours.
        """
        if self.state.session is not None:
            return
        merged = self.merged_now()
        if merged is None:
            return

        now = self.clock.wall()
        remaining = merged.ends_at - now
        if remaining <= 0:  # pragma: no cover - active_at would not have said so
            return

        session = start_session(
            profile=merged.profile.name,
            level=merged.level,
            duration_seconds=remaining,
            valve=merged.valve,
            origin=SessionOrigin.SCHEDULE,
            clock=self.clock,
            policy=self.policy,
        )
        session = replace(session, schedule_ids=merged.schedule_ids)
        session = session.with_breaks(BreakState.start(merged.profile.breaks, self.clock))

        self.state.session = session
        self.save()
        self._match_refusal()
        self.stats.session_started(
            session_id=session.id,
            profile=session.profile,
            level=str(session.level),
            origin=str(session.origin),
            at=session.anchor.started_at,
        )
        self.emit("session.started", self.status())
        log.info(
            "schedule %s started session %s until %s",
            ", ".join(merged.schedule_ids),
            session.id,
            merged.ends_at,
        )

    def _scheduled_window_closed(self, session: Session) -> bool:
        """Whether a scheduled session's window has passed.

        The session's own clock would end it anyway, since its duration is the
        window. This catches the other way round: a schedule deleted or
        disabled while it was running, which should stop blocking rather than
        run to a deadline nobody asked for any more.
        """
        if session.origin is not SessionOrigin.SCHEDULE or not session.schedule_ids:
            return False
        return not self.current_occurrences()

    def _forget_old_skips(self, now: float) -> None:
        stale = [key for key, ends_at in self.state.skipped.items() if ends_at <= now]
        for key in stale:
            del self.state.skipped[key]

    def _session_profile(self, session: Session) -> Profile | None:
        """The rules a session is running under.

        For a scheduled session this is worked out from the schedules rather
        than read from a stored copy, so that one place decides what a
        schedule blocks.
        """
        if session.origin is SessionOrigin.SCHEDULE and session.schedule_ids:
            merged = self.merged_now()
            if merged is not None:
                return merged.profile
        return self.profiles.get(session.profile)

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
            "break.skip": self._on_break_skip,
            "break.postpone": self._on_break_postpone,
            "profile.list": self._on_profile_list,
            "profile.show": self._on_profile_show,
            "profile.create": self._on_profile_create,
            "profile.edit": self._on_profile_edit,
            "profile.delete": self._on_profile_delete,
            "policy.get": self._on_policy,
            "category.list": self._on_category_list,
            "schedule.list": self._on_schedule_list,
            "schedule.show": self._on_schedule_show,
            "schedule.create": self._on_schedule_create,
            "schedule.edit": self._on_schedule_edit,
            "schedule.delete": self._on_schedule_delete,
            "schedule.skip": self._on_skip,
            "doctor.run": self._on_doctor,
            "config.get": self._on_config_get,
            "config.set": self._on_config_set,
            "stats.query": self._on_stats_query,
            "stats.delete": self._on_stats_delete,
            "blocked.report": self._on_blocked_report,
            "apps.report": self._on_apps_report,
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
        # The blocker speaks only when it polls, so this is where its being
        # alive is recorded (SPEC 15, `anchor doctor`).
        self._blocker_seen = self.clock.wall()
        session = self.state.session
        if session is None:
            return request.ok({"active": False})

        profile = self._session_profile(session)
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
                    # Nothing can be said about applications: the list of them
                    # lived in the profile that vanished, and closing every
                    # application on the machine is not a safe guess.
                    "apps": [],
                    "grace_seconds": self._grace_remaining(session),
                    # A profile that vanished must not become a way to keep a
                    # tunnel up either.
                    "block_tunnels": session.level is Level.STRICT,
                }
            )

        bundled = resolve(profile.categories, self.categories)

        # SPEC 10: a break does not unblock anything unless the profile says
        # so, and even then only sites. Applications stay blocked, because
        # five minutes is long enough to lose an hour in one.
        resting = session.phase is SessionPhase.BREAK
        if resting and profile.breaks.allow_sites_during_breaks:
            return request.ok(
                {
                    "active": True,
                    "mode": str(WebMode.BLOCKLIST),
                    "domains": [],
                    "apps": sorted(profile.apps | bundled.apps),
                    "grace_seconds": self._grace_remaining(session),
                    "block_tunnels": (session.level is Level.STRICT and profile.block_vpn_and_tor),
                }
            )

        return request.ok(
            {
                "active": True,
                "mode": str(profile.web_mode),
                # A category's domains are things to block, so in allowlist
                # mode they are left out: adding them to the list of what is
                # allowed would turn "block social media" into "social media
                # is the only thing you may read". Its applications still
                # apply, because there is no allowlist for those (SPEC 9).
                "domains": sorted(
                    profile.domains | bundled.domains
                    if profile.web_mode is WebMode.BLOCKLIST
                    else profile.domains
                ),
                "apps": sorted(profile.apps | bundled.apps),
                # How long the applications already open still have to save
                # their work (SPEC 7.1). The engine works it out rather than
                # the blocker, so that restarting the blocker cannot hand out
                # a fresh two minutes.
                "grace_seconds": self._grace_remaining(session),
                # Two conditions, and the engine owns both. SPEC 7.2 allows VPN
                # and Tor below Strict, and ADR 4 lets a profile opt out of
                # blocking them even in Strict.
                "block_tunnels": session.level is Level.STRICT and profile.block_vpn_and_tor,
            }
        )

    def _on_category_list(self, request: Request) -> Response:
        """The categories this machine knows (SPEC 12).

        The interface needs their names to show tick boxes, and a profile
        stores only their identifiers.
        """
        return request.ok(
            {"categories": [category.to_dict() for category in _by_name(self.categories)]}
        )

    def _on_skip(self, request: Request) -> Response:
        """Skip the scheduled session that is running (SPEC 11).

        Three a week, reset on Monday, and every one is a rupture. A Strict
        scheduled session cannot be skipped at all: its valve is the only way
        out, chosen when the schedule was written.
        """
        session = self._require_session()
        if session.origin is not SessionOrigin.SCHEDULE:
            raise AnchorError(
                "this session was started by hand; skipping is for scheduled ones",
                code=ErrorCode.SKIP_FORBIDDEN,
            )
        if session.level is Level.STRICT:
            raise AnchorError(
                "a Strict scheduled session cannot be skipped; only its valve applies",
                code=ErrorCode.SKIP_FORBIDDEN,
            )

        self._reset_skips_if_new_week()
        if self.state.skips_remaining <= 0:
            raise AnchorError(
                f"no skips left this week; they come back on Monday " f"({SKIPS_PER_WEEK} a week)",
                code=ErrorCode.SKIP_LIMIT_REACHED,
            )

        # Remember the occurrence, or the next tick starts it again a second
        # later, which is not what anyone means by skipping.
        for occurrence in self.current_occurrences():
            self.state.skipped[occurrence.id] = occurrence.ends_at

        self.state.skips_used += 1
        self._record_rupture(
            RuptureKind.SKIP, f"the scheduled session {session.profile!r} was skipped"
        )
        self._end_session(reason="skipped")
        return request.ok({"skipped": True, **self.status()})

    def _reset_skips_if_new_week(self) -> None:
        """Skips come back on Monday at 00:00 local time (SPEC 11)."""
        today = datetime.fromtimestamp(self.clock.wall()).date()
        monday = (today - timedelta(days=today.weekday())).isoformat()
        if self.state.skips_week_start != monday:
            self.state.skips_week_start = monday
            self.state.skips_used = 0

    def _on_schedule_list(self, request: Request) -> Response:
        active = {occurrence.id for occurrence in self.current_occurrences()}
        return request.ok(
            {
                "schedules": [
                    {**schedule.to_dict(), "active": schedule.id in active}
                    for schedule in _by_start(self.schedules.values())
                ],
                "skips_remaining": self.state.skips_remaining,
            }
        )

    def _on_schedule_show(self, request: Request) -> Response:
        schedule = self._find_schedule(str(request.payload["id"]))
        active = {occurrence.id for occurrence in self.current_occurrences()}
        return request.ok({"schedule": {**schedule.to_dict(), "active": schedule.id in active}})

    def _on_schedule_create(self, request: Request) -> Response:
        payload = request.payload
        profile = str(payload["profile"])
        if self.profiles and profile not in self.profiles:
            raise AnchorError(
                f"there is no profile called {profile!r}", code=ErrorCode.UNKNOWN_PROFILE
            )

        try:
            schedule = Schedule.create(
                name=str(payload["name"]),
                profile=profile,
                days=frozenset(parse_day(day) for day in payload["days"]),
                start_minute=parse_clock(str(payload["start"])),
                end_minute=parse_clock(str(payload["end"])),
                level=Level(payload.get("level") or Level.SOFT),
                valve=Valve(payload["valve"]) if payload.get("valve") else None,
            )
        except ValueError as error:
            raise AnchorError(str(error), code=ErrorCode.BAD_REQUEST) from error

        self.schedules[schedule.id] = schedule
        self.save_config()
        self.emit("config.changed", {"what": "schedules"})
        return request.ok({"schedule": schedule.to_dict()})

    def _on_schedule_edit(self, request: Request) -> Response:
        """Change a schedule that is not running (SPEC 11).

        Before it starts it can be edited freely; while it is running it
        cannot, for the same reason a profile in use cannot be loosened. The
        way out of a session is the session's own, not the schedule's.
        """
        payload = request.payload
        schedule = self._find_schedule(str(payload["id"]))
        self._refuse_if_running(schedule, "edited")

        changes: dict[str, Any] = {}
        if payload.get("name"):
            changes["name"] = str(payload["name"])
        if payload.get("profile"):
            changes["profile"] = str(payload["profile"])
        if payload.get("days"):
            changes["days"] = frozenset(parse_day(day) for day in payload["days"])
        if payload.get("start"):
            changes["start_minute"] = parse_clock(str(payload["start"]))
        if payload.get("end"):
            changes["end_minute"] = parse_clock(str(payload["end"]))
        if payload.get("level"):
            changes["level"] = Level(payload["level"])
        if payload.get("valve"):
            changes["valve"] = Valve(payload["valve"])
        if payload.get("enabled") is not None:
            changes["enabled"] = bool(payload["enabled"])
        if not changes:
            raise AnchorError("nothing to change", code=ErrorCode.BAD_REQUEST)

        try:
            updated = replace(schedule, **changes)
        except (ValueError, AnchorError) as error:
            raise AnchorError(str(error), code=ErrorCode.BAD_REQUEST) from error

        self.schedules[updated.id] = updated
        self.save_config()
        self.emit("config.changed", {"what": "schedules"})
        return request.ok({"schedule": updated.to_dict()})

    def _on_schedule_delete(self, request: Request) -> Response:
        schedule = self._find_schedule(str(request.payload["id"]))
        self._refuse_if_running(schedule, "deleted")

        del self.schedules[schedule.id]
        self.save_config()
        self.emit("config.changed", {"what": "schedules"})
        return request.ok({"deleted": schedule.id})

    def _find_schedule(self, identifier: str) -> Schedule:
        schedule = self.schedules.get(identifier)
        if schedule is None:
            raise AnchorError(
                f"there is no schedule with the identifier {identifier!r}",
                code=ErrorCode.UNKNOWN_SCHEDULE,
            )
        return schedule

    def _refuse_if_running(self, schedule: Schedule, what: str) -> None:
        if any(occurrence.id == schedule.id for occurrence in self.current_occurrences()):
            raise AnchorError(
                f"{schedule.name!r} is running now and cannot be {what}; "
                "a schedule is changed before it starts, not during",
                code=ErrorCode.RATCHET_VIOLATION,
            )

    def _on_stats_query(self, request: Request) -> Response:
        """One range of statistics (SPEC 13).

        Today is taken from the engine's clock rather than from the caller,
        so that two clients asking at the same moment cannot disagree about
        what day it is.
        """
        view = str(request.payload["range"])
        today = datetime.fromtimestamp(self.clock.wall()).date()
        return request.ok(summarise(self.stats, view, today=today).to_dict())

    def _on_stats_delete(self, request: Request) -> Response:
        """Delete every statistic (SPEC 13).

        Allowed during a session, and not subject to the ratchet. Statistics
        are a record of what Anchor did, not part of what it is enforcing, and
        a person who wants their own history gone should not have to wait for
        a session to end to be rid of it.
        """
        self.stats.delete_everything()
        return request.ok({"deleted": True})

    def _on_doctor(self, request: Request) -> Response:
        """Look at the machine and say what is wrong (SPEC 15).

        The indicator check is missing from this answer on purpose: the panel
        lives in the user's session and the engine runs outside it, so the
        client that asked adds that one itself. Guessing from here would be
        the engine reporting on something it cannot see.
        """
        facts = gather(
            session_active=self.state.session is not None,
            blocker_last_seen=self._blocker_seen,
            now=self.clock.wall(),
            observer=Observer(),
        )
        checks = examine(facts)
        return request.ok(
            {
                "checks": [check.to_dict() for check in checks],
                "verdict": worst(checks),
            }
        )

    # -- settings ---------------------------------------------------------

    def _on_config_get(self, request: Request) -> Response:
        """One setting, or all of them (SPEC 15).

        Always with the effective value and whether it is still the default,
        because "15 minutes" and "15 minutes, because nobody has chosen" are
        different things to show in a Settings screen.
        """
        described = describe(
            self.preferences,
            policy=self._base_policy,
            installed_retention=self.settings.retention_days,
        )
        key = request.payload.get("key")
        if key is None:
            return request.ok({"settings": described})

        for entry in described:
            if entry["key"] == key:
                return request.ok({"settings": [entry]})
        known = ", ".join(sorted(PREFERENCES))
        raise AnchorError(
            f"there is no setting called {key!r}. Anchor knows: {known}",
            code=ErrorCode.INVALID_CONFIG,
        )

    def _on_config_set(self, request: Request) -> Response:
        """Change one setting, unless a session forbids it (SPEC 7.2)."""
        key = str(request.payload["key"])
        setting = PREFERENCES.get(key)
        if setting is not None and not setting.during_session and self.state.session is not None:
            raise AnchorError(
                f"{key} cannot be changed while a session is running. "
                "It decides how hard this session is to leave, "
                "and that is settled when the session starts.",
                code=ErrorCode.SETTING_LOCKED,
            )

        # An unknown key is refused here, by the same words the reader uses.
        self.preferences = self.preferences.set(key, str(request.payload["value"]))
        self._apply_preferences()
        self.save_config()
        self.emit("config.changed", {"what": "settings", "key": key})
        return request.ok({"key": key, "value": self.preferences.get(key)})

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
        self.stats.attempt(
            session_id=session.id, kind="domain", target=domain, at=self.clock.wall()
        )
        # This event carries a domain, and goes only to subscribers in the
        # user's own session so the agent can show a notification. It is never
        # written to the log (SPEC 13).
        self.emit("blocked.attempt", {"domain": domain, "rule": rule})
        return request.ok({"recorded": True, "total": self.state.session.blocked_attempts})

    def _grace_remaining(self, session: Session) -> float:
        """Seconds left of the grace this session began with (SPEC 7.1)."""
        outcome = reconcile(session.anchor, self.clock)
        elapsed = session.anchor.duration_seconds - outcome.remaining_seconds
        return max(0.0, GRACE_SECONDS - elapsed)

    def _on_apps_report(self, request: Request) -> Response:
        """Record what the blocker did about applications (SPEC 7.1, 9).

        The blocker closes them itself, for the same reason it puts the
        firewall rules back itself: waiting for an instruction would leave a
        blocked application open meanwhile. What it cannot do is decide what
        the user should be told, so it reports and the engine publishes.
        """
        session = self.state.session
        if session is None:
            # The session ended between the kill and the report.
            return request.ok({"recorded": False})

        kind = str(request.payload["kind"])
        names = [str(name) for name in request.payload["apps"]]
        seconds = int(request.payload["seconds"])

        if kind == "grace":
            # Named applications, and only to the owner's own subscribers, on
            # the same footing as a blocked domain (SPEC 13).
            self.emit("apps.grace", {"apps": names, "seconds": seconds})
            return request.ok({"recorded": True})

        self.state.session = session.with_app_blocks(len(names))
        self.save()
        for name in names:
            self.stats.attempt(session_id=session.id, kind="app", target=name, at=self.clock.wall())
        # "launch" means the user opened it during the session and it never
        # got a window; "closed" means it was already open when the session
        # began. The same event, because it is the same fact, with the reason
        # carried so the agent can word it properly.
        self.emit("apps.closed", {"apps": names, "reason": kind})
        return request.ok({"recorded": True, "total": self.state.session.app_blocks})

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

    def _advance_breaks(self, session: Session) -> None:
        """Move the work-and-rest pattern on (SPEC 10).

        A session whose profile has been uninstalled keeps its blocks and
        stops changing phase: there is no pattern to follow, and guessing one
        would interrupt the user on the strength of a guess.
        """
        profile = self._session_profile(session)
        if session.breaks is None or profile is None:
            return

        outcome = advance_breaks(session.breaks, profile.breaks, self.clock)
        if outcome.state == session.breaks and not outcome.events:
            return

        self._record_breaks(session.id, session.breaks, outcome.state)
        self.state.session = session.with_breaks(outcome.state)
        self.save()
        for name, payload in outcome.events:
            self.emit(name, {**payload, "phase": str(outcome.state.phase)})

    def _record_breaks(self, session_id: str, before: BreakState, after: BreakState) -> None:
        """Write down what changed, rather than what was announced (SPEC 13).

        Reading the counters instead of the events catches the break nobody
        saw: an absence that covers one counts it as taken and publishes
        nothing, and a statistic that only counted announcements would quietly
        lose it.
        """
        for outcome, count in (
            ("taken", after.taken - before.taken),
            ("postponed", after.postponed_total - before.postponed_total),
            ("skipped", after.skipped - before.skipped),
        ):
            for _ in range(max(0, count)):
                self.stats.break_outcome(
                    session_id=session_id, outcome=outcome, at=self.clock.wall()
                )

    def _on_break_skip(self, request: Request) -> Response:
        """Give up the break now running, if the profile allows it (SPEC 10)."""
        session, profile, state = self._break_context()
        skipped = skip_break(state, profile.breaks, self.clock)
        self._record_breaks(session.id, state, skipped)
        self.state.session = session.with_breaks(skipped)
        self.save()
        self.emit(
            "break.ended",
            {
                "skipped": True,
                "type": str(profile.breaks.type),
                "phase": str(SessionPhase.WORKING),
            },
        )
        return request.ok(self.status())

    def _on_break_postpone(self, request: Request) -> Response:
        """Push the break back, if the profile allows it (SPEC 10)."""
        session, profile, state = self._break_context()
        moved = postpone_break(state, profile.breaks, self.clock)
        self._record_breaks(session.id, state, moved)
        self.state.session = session.with_breaks(moved)
        self.save()
        self.emit(
            "break.ended",
            {
                "postponed": True,
                "seconds": moved.remaining(self.clock),
                "type": str(profile.breaks.type),
                "phase": str(SessionPhase.WORKING),
            },
        )
        return request.ok(self.status())

    def _break_context(self) -> tuple[Session, Profile, BreakState]:
        """The session, profile and break state, or a refusal saying which is missing."""
        self.tick()
        session = self.state.session
        if session is None:
            raise AnchorError("no session is running", code=ErrorCode.NO_ACTIVE_SESSION)
        if session.breaks is None:
            raise AnchorError(
                "this session has no break pattern, because its profile is not installed",
                code=ErrorCode.UNKNOWN_PROFILE,
            )
        profile = self._session_profile(session)
        if profile is None:
            raise AnchorError(
                f"there is no profile called {session.profile!r}",
                code=ErrorCode.UNKNOWN_PROFILE,
            )
        return session, profile, session.breaks

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
            "app_blocks": session.app_blocks,
            "skips_remaining": self.state.skips_remaining,
            "break": self._break_status(session),
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

    def _break_status(self, session: Session) -> dict[str, Any] | None:
        """What the indicator, the overlay and the interface need (SPEC 10, 14.1).

        One shape whether a break is running or not: the difference is the
        phase, and a client that has to ask two questions to draw one line
        eventually asks them at two different instants.
        """
        state = session.breaks
        if state is None:
            return None

        profile = self._session_profile(session)
        settings = profile.breaks if profile is not None else None
        return {
            "phase": str(state.phase),
            "remaining_seconds": state.remaining(self.clock),
            "ends_at": state.ends_at,
            "long": state.long,
            "taken": state.taken,
            "postponed": state.postponed_total,
            "skipped": state.skipped,
            "cycles_done": state.cycles_done,
            "type": str(settings.type) if settings else None,
            "hardness": str(settings.hardness) if settings else None,
            "can_skip": bool(settings and settings.hardness.can_skip),
            "can_postpone": bool(
                settings
                and (
                    settings.hardness.postpone_limit is None
                    or state.postponed < settings.hardness.postpone_limit
                )
            ),
            "allow_sites": bool(settings and settings.allow_sites_during_breaks),
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
        profile = self.profiles.get(profile_name)
        if profile is not None:
            # The pattern belongs to the profile (SPEC 10, 12), so a session
            # whose profile is not installed has no breaks rather than default
            # ones: inventing a pattern would be inventing an interruption.
            session = session.with_breaks(BreakState.start(profile.breaks, self.clock))

        self.state.session = session
        self.save()
        self._match_refusal()

        self.stats.session_started(
            session_id=session.id,
            profile=session.profile,
            level=str(session.level),
            origin=str(session.origin),
            at=session.anchor.started_at,
        )
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

        self.stats.session_ended(
            session_id=session.id,
            started_at=session.anchor.started_at,
            ended_at=self.clock.wall(),
            reason=reason,
        )
        # The window moves with every session, so this is where it is swept.
        # Pruning on a timer would mean a timer that exists to delete things.
        self.stats.prune(now=self.clock.wall())

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
        session = self.state.session
        self.stats.rupture(
            session_id=session.id if session else None,
            kind=str(rupture.kind),
            at=rupture.at,
        )
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


def _by_name(categories: dict[str, Category]) -> list[Category]:
    return sorted(categories.values(), key=lambda category: (category.name.lower(), category.id))


def _by_start(schedules: Any) -> list[Schedule]:
    """Schedules in the order a person reads a week."""
    return sorted(schedules, key=lambda item: (min(item.days), item.start_minute, item.name))
