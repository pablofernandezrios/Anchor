"""The blocker daemon: applying and undoing a session's blocks (SPEC 5.1).

The engine decides; this executes. The daemon asks the engine what is in force,
and when that changes it brings the machine into line: rules loaded, resolver
pointed at the right upstreams, browser policies written, and all of it undone
when the session ends.

It asks rather than listens. An event stream would be tidier, but the blocker
has to be right about the current state even when it has just been restarted,
when the engine has just been restarted, and when a package upgrade restarted
both in the wrong order. Asking a short question on a timer is correct in all
three cases and needs no reconnection logic.

The daemon never decides anything about the session itself. It has no opinion
on levels, the ratchet or when a session ends; it asks, and it obeys.
"""

from __future__ import annotations

import logging
import pwd
import threading
from dataclasses import dataclass
from pathlib import Path

from anchor.blocker.apps import InstalledApp, discover
from anchor.blocker.attempts import AttemptTracker
from anchor.blocker.constants import JOURNAL_NAME, RESOLVED_DROP_IN, RESOLVER_PORT
from anchor.blocker.enforcement import AppEnforcer, Closure
from anchor.blocker.journal import Journal
from anchor.blocker.matcher import Policy, load_domain_file
from anchor.blocker.policies import apply_policies
from anchor.blocker.recent import RecentAnswers
from anchor.blocker.resolved import (
    NetworkState,
    discover_upstreams,
    follow_network_changes,
    is_available,
)
from anchor.blocker.resolved import apply as apply_resolved
from anchor.blocker.resolver import Resolver, ResolverConfig
from anchor.blocker.restore import restore_everything
from anchor.blocker.rules import (
    FirewallPlan,
    apply_rules,
    load_addresses,
    load_tunnel_ports,
    rules_loaded,
    split_addresses,
)
from anchor.blocker.watcher import ProcessWatcher
from anchor.cli.client import EngineClient, EngineUnreachableError
from anchor.engine.paths import Paths, Settings
from anchor.protocol.types import WebMode
from anchor.system.commands import Runner, run

log = logging.getLogger("anchor-blockerd")

#: Where the shipped lists live once installed.
DATA_DIR = Path("/usr/share/anchor")

#: How often to ask the engine what is in force.
POLL_SECONDS = 1.0

#: How often to check whether the network moved. Less often than the policy
#: poll, because it shells out to resolvectl.
NETWORK_POLL_SECONDS = 5.0


@dataclass(slots=True)
class Lists:
    """The lists Anchor ships, which are the blocker's business not the user's."""

    essentials: frozenset[str] = frozenset()
    doh_domains: frozenset[str] = frozenset()
    doh_v4: list[str] | None = None
    doh_v6: list[str] | None = None
    tunnel_ports: list[tuple[str, int]] | None = None

    @classmethod
    def load(cls, data_dir: Path = DATA_DIR) -> Lists:
        v4, v6 = split_addresses(load_addresses(data_dir / "doh-endpoints.txt"))
        return cls(
            essentials=load_domain_file(data_dir / "essentials.txt"),
            doh_domains=load_domain_file(data_dir / "doh-domains.txt"),
            doh_v4=v4,
            doh_v6=v6,
            tunnel_ports=load_tunnel_ports(data_dir / "tunnels.txt"),
        )


class BlockerDaemon:
    """Keeps the machine matching whatever session the engine reports."""

    def __init__(
        self,
        paths: Paths,
        *,
        lists: Lists | None = None,
        runner: Runner = run,
        resolver_port: int = RESOLVER_PORT,
        policy_root: Path | None = None,
        resolved_drop_in: Path = RESOLVED_DROP_IN,
    ) -> None:
        self.paths = paths
        self.lists = lists if lists is not None else Lists.load()
        self.runner = runner
        self.resolver_port = resolver_port
        self.policy_root = policy_root
        self.resolved_drop_in = resolved_drop_in

        self.journal = Journal(paths.state_dir / JOURNAL_NAME)
        self.recent = RecentAnswers()
        self.attempts = AttemptTracker()

        self._policy: Policy | None = None
        self._applied = False
        self._looked_for_leftovers = False
        self._blocking_tunnels = False
        self._network = NetworkState()
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._resolver: Resolver | None = None

        self.enforcer = AppEnforcer(
            catalogue=self.installed_apps,
            on_grace=self.report_grace,
            on_closed=self.report_closed,
        )
        self._watcher: ProcessWatcher | None = None

    # -- what the resolver asks --------------------------------------------

    def current_policy(self) -> Policy | None:
        with self._lock:
            return self._policy

    def on_blocked(self, domain: str, rule: str) -> None:
        """Report a refused name, at most as often as SPEC 8.3 allows."""
        outcome = self.attempts.record(domain)
        if not outcome.counted:
            return
        try:
            with EngineClient(self.paths.engine_socket, timeout=2.0) as client:
                client.call("blocked.report", {"domain": domain, "rule": rule})
        except (EngineUnreachableError, OSError):
            # The engine is restarting. The block already happened; losing one
            # statistic is not worth failing a lookup over.
            log.debug("could not report a blocked attempt; the engine is unreachable")

    # -- what the enforcer asks and reports ---------------------------------

    def installed_apps(self) -> list[InstalledApp]:
        """Every application installed for the owner (SPEC 9).

        The daemon runs as root, so the owner's own ``~/.local`` entries have
        to be asked for by name; discovering root's would find nothing the
        user has installed for themselves.
        """
        return discover(home=self.owner_home())

    def owner_home(self) -> Path | None:
        try:
            settings = Settings.load(self.paths.settings_file)
            return Path(pwd.getpwuid(settings.owner_uid).pw_dir)
        except (FileNotFoundError, ValueError, KeyError) as error:
            log.warning("could not find the owner's home directory: %s", error)
            return None

    def report_grace(self, apps: list[InstalledApp], seconds: float) -> None:
        """Tell the engine what is about to close, so it can warn (SPEC 7.1)."""
        self.tell_engine(
            "apps.report",
            {
                "kind": "grace",
                "apps": [app.name for app in apps],
                "seconds": int(seconds),
            },
        )

    def report_closed(self, closures: list[Closure], reason: str) -> None:
        self.tell_engine(
            "apps.report",
            {
                "kind": "launch" if reason == "launch" else "closed",
                "apps": [closure.name for closure in closures],
            },
        )

    def tell_engine(self, request: str, payload: dict[str, object]) -> None:
        try:
            with EngineClient(self.paths.engine_socket, timeout=2.0) as client:
                client.call(request, payload)
        except (EngineUnreachableError, OSError):
            # The engine is restarting. The application is already closed;
            # losing the notification is not worth failing over.
            log.debug("could not send %s; the engine is unreachable", request)

    @property
    def applied(self) -> bool:
        """Whether blocks are currently in place."""
        return self._applied

    # -- the loop -----------------------------------------------------------

    def start_resolver(self) -> None:
        config = ResolverConfig(port=self.resolver_port)
        self._resolver = Resolver(
            config,
            policy=self.current_policy,
            on_blocked=self.on_blocked,
            recent=self.recent,
        )
        self._resolver.start()

    def stop(self) -> None:
        self._stopping.set()
        if self._resolver is not None:
            self._resolver.stop()
            self._resolver = None
        self.stop_enforcing()

    def run(self) -> None:
        """Poll the engine until stopped, applying and undoing as it says."""
        self.start_resolver()
        since_network = 0.0

        while not self._stopping.wait(POLL_SECONDS):
            try:
                self.poll()
            except Exception:
                log.exception("the policy poll failed; will try again")

            since_network += POLL_SECONDS
            if self._applied and since_network >= NETWORK_POLL_SECONDS:
                since_network = 0.0
                try:
                    self.follow_network()
                except Exception:
                    log.exception("following the network failed; will try again")

    def poll(self) -> None:
        """Ask the engine what is in force and act on any change."""
        try:
            with EngineClient(self.paths.engine_socket, timeout=3.0) as client:
                response = client.call("policy.get")
        except EngineUnreachableError:
            # The engine is down. Rules already in place stay in place: during
            # an active block, traffic stays blocked until things recover (P4).
            log.debug("the engine is unreachable; leaving the current state alone")
            return

        if not response.ok:
            log.warning("the engine refused policy.get: %s", response.error.get("message"))
            return

        active = bool(response.result.get("active"))
        if active:
            self.check_rules_survive()
            self.apply(
                WebMode(response.result.get("mode", WebMode.BLOCKLIST)),
                frozenset(response.result.get("domains", ())),
                block_tunnels=bool(response.result.get("block_tunnels", False)),
            )
            self.enforce(
                frozenset(response.result.get("apps", ())),
                float(response.result.get("grace_seconds", 0.0)),
            )
            return

        self.stop_enforcing()
        if self._applied:
            self.undo()
            return
        self.forget_leftovers()

    def forget_leftovers(self) -> bool:
        """Clear blocks that outlived the daemon that applied them (P4).

        A SIGKILL, a power cut or an upgrade in the middle of a session leaves
        the nftables table loaded and the managed browser policies written.
        The session is over, nobody is watching them, and until a reboot or an
        `anchor-blockerd --restore` the machine is being blocked by nothing.
        Anchor's promise is that it fails open, so this closes the one hole
        where it did not.

        Looked for once, when the engine first says no session is running, and
        never again: `nft` once at startup is nothing, and once a second for
        the rest of the login is a process a second forever. A daemon that
        restarts into a live session never reaches this, because the poll that
        reports a session returns before it.
        """
        if self._looked_for_leftovers or self._applied:
            # Blocks this daemon applied are undone by the ordinary path,
            # which knows what it wrote and what to put back.
            return False
        self._looked_for_leftovers = True

        if not rules_loaded(runner=self.runner):
            return False

        log.warning(
            "found Anchor's firewall table loaded with no session running; "
            "something stopped without cleaning up. Undoing it"
        )
        self.undo()
        return True

    def check_rules_survive(self) -> None:
        """Notice if Anchor's firewall table has been removed (SPEC 7.6).

        Deleting the table is the simplest way to walk out of a session, and
        it takes one command. It is detected rather than prevented, because
        root can always do it: the rules go back, and the attempt is recorded
        as a rupture, which is the whole of what P2 promises.
        """
        if not self._applied or rules_loaded(runner=self.runner):
            return

        log.warning("Anchor's firewall table is gone; putting it back")
        self.report_tampering(
            "rules_missing",
            "the inet anchor table was removed while a session was running",
        )
        # Forget what was applied so the next apply() rebuilds it rather than
        # deciding nothing has changed.
        self._applied = False
        with self._lock:
            self._policy = None

    def report_tampering(self, kind: str, detail: str) -> None:
        try:
            with EngineClient(self.paths.engine_socket, timeout=2.0) as client:
                client.call("tamper.report", {"kind": kind, "detail": detail})
        except (EngineUnreachableError, OSError):
            log.warning("could not report tampering; the engine is unreachable")

    # -- applications ---------------------------------------------------------

    def enforce(self, app_ids: frozenset[str], grace_seconds: float) -> None:
        """Keep the session's applications closed (SPEC 7.1, 9).

        The watcher starts with the session rather than with the first block,
        so that its baseline is taken while the grace is still running: an
        application opened during those two minutes is then a launch, and not
        something that looks as if it had been running all along.
        """
        if self._watcher is None:
            self._watcher = ProcessWatcher(self.enforcer.on_launch)
            self._watcher.start()
        self.enforcer.update(app_ids, grace_seconds)

    def stop_enforcing(self) -> None:
        """Let the applications alone again. Safe to call when idle."""
        if self._watcher is not None:
            self._watcher.stop()
            self._watcher = None
        self.enforcer.stop()

    # -- the two transitions -------------------------------------------------

    def apply(self, mode: WebMode, domains: frozenset[str], *, block_tunnels: bool = False) -> None:
        """Bring the machine into line with a session."""
        # The DoH domains are added to whatever the user blocks, in both modes.
        # In blocklist mode they are extra rules; in allowlist mode they would
        # already be blocked, and adding them costs nothing.
        blocked_domains = domains | self.lists.doh_domains if mode is WebMode.BLOCKLIST else domains

        policy = Policy(mode=mode, domains=blocked_domains, essentials=self.lists.essentials)

        with self._lock:
            unchanged = (
                self._applied and self._policy == policy and self._blocking_tunnels == block_tunnels
            )
            self._policy = policy

        if unchanged:
            return

        # Somewhere to forward to must exist before anything is redirected.
        # A redirect to a resolver with no upstream turns a focus session into
        # a total loss of name resolution, which is far worse than not
        # blocking, and the user would have no way to tell which had happened.
        upstreams = discover_upstreams(runner=self.runner, resolver_port=self.resolver_port)
        if not upstreams:
            log.error(
                "no upstream DNS server could be found, in systemd-resolved or in "
                "/etc/resolv.conf. Not redirecting DNS: doing so would stop every "
                "name on this machine from resolving. Blocking is NOT active"
            )
            with self._lock:
                self._policy = None
            return

        if self._resolver is not None:
            self._resolver.set_upstreams(upstreams)

        # Addresses resolved before the session started, for names it blocks.
        stale = self.recent.addresses_for(policy.is_blocked)
        blocked_v4, blocked_v6 = split_addresses(stale)

        apply_rules(
            FirewallPlan(
                resolver_port=self.resolver_port,
                doh_v4=self.lists.doh_v4 or [],
                doh_v6=self.lists.doh_v6 or [],
                blocked_v4=blocked_v4,
                blocked_v6=blocked_v6,
                tunnel_ports=(self.lists.tunnel_ports or []) if block_tunnels else [],
            ),
            self.journal,
            runner=self.runner,
        )

        if is_available(runner=self.runner):
            self._network = apply_resolved(
                self.journal,
                runner=self.runner,
                resolver_port=self.resolver_port,
                drop_in=self.resolved_drop_in,
                # What resolved reported a moment ago, for the case where a
                # previous session's drop-in is still in place and the only
                # servers left to read are Anchor's own.
                known_upstreams=upstreams,
            )
            if self._resolver is not None:
                self._resolver.set_upstreams(self._network.upstreams)
        else:
            log.info(
                "systemd-resolved is not running here; the firewall redirect still "
                "catches every query and the upstreams come from /etc/resolv.conf, "
                "which is the path for distributions without it"
            )

        apply_policies(self.journal, root=self.policy_root)

        self._applied = True
        log.info("blocking active: %s with %d domain(s)", mode, len(blocked_domains))

    def undo(self) -> None:
        """Put everything back when the session ends."""
        self.stop_enforcing()
        report = restore_everything(
            self.journal, runner=self.runner, resolved_drop_in=self.resolved_drop_in
        )

        with self._lock:
            self._policy = None
        self._applied = False
        self._blocking_tunnels = False
        # The map is a record of what the user looked at, so it does not outlive
        # the session that needed it.
        self.recent.clear()
        self.attempts.reset()
        self._network = NetworkState()

        log.info("blocking stopped: %s", report.summary())

    def follow_network(self) -> None:
        """Re-apply if the machine changed networks (SPEC 3, 8.2)."""
        if not is_available(runner=self.runner):
            return
        updated = follow_network_changes(
            self.journal,
            self._network,
            runner=self.runner,
            resolver_port=self.resolver_port,
            drop_in=self.resolved_drop_in,
        )
        if updated is not None:
            self._network = updated
            if self._resolver is not None and updated.upstreams:
                # Never replace working servers with none. A re-apply that
                # found nothing to read has already said so; forwarding to an
                # empty list would stop every name on the machine resolving.
                self._resolver.set_upstreams(updated.upstreams)
