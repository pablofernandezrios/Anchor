"""``anchor doctor``: what is wrong, and what to type (SPEC 15).

SPEC 15 names five things to check — the DNS path, the nftables table, the
browser policies, the daemons and the indicator extension — and one thing to
do with them: print fixes. A check that reports a problem without saying what
to do about it has told the user something they already suspected.

The module is in two halves, for the usual reason. :func:`gather` touches
nftables, systemd-resolved and ``/etc``, and cannot be tested anywhere but on
a real machine. :func:`examine` decides what the findings mean, and is
ordinary arithmetic over a dataclass.

Two judgements are worth spelling out, because they are the ones a naive
version gets backwards.

**What "healthy" means depends on whether a session is running.** Anchor
applies its firewall table, its resolver drop-in and its browser policies when
a session starts and undoes all of it when the session ends. So a loaded table
is correct during a session and is *wrong* outside one: it means something
died without cleaning up, and the machine is being blocked by nobody. That is
a real failure with a real fix, and it is the failure this command exists to
catch.

**A missing indicator extension is a note, not a problem.** SPEC 14.1 wants
the time left in the top bar, but it is also on the Home screen and in
``anchor status``. Anchor blocks exactly as well without it, and a doctor that
cried failure over a panel icon would train its user to ignore it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from anchor.blocker.constants import RESOLVER_PORT
from anchor.blocker.policies import BROWSERS
from anchor.blocker.resolved import discover_upstreams as _discover_upstreams
from anchor.blocker.resolved import is_available, read_resolv_conf
from anchor.blocker.rules import rules_loaded
from anchor.cli.durations import format_duration
from anchor.system.commands import Runner, run

#: How long the blocker may be silent before it counts as gone. It polls once
#: a second; ten times that is a machine under load, not a daemon that died.
BLOCKER_SILENCE_SECONDS: Final = 15.0

#: Where systemd-resolved reads Anchor's drop-in. Matches the blocker's.
RESOLVED_DROP_IN: Final = Path("/run/systemd/resolved.conf.d/anchor.conf")

RESTORE_FIX: Final = "sudo anchor-blockerd --restore"


@dataclass(frozen=True, slots=True)
class BrowserFact:
    """One browser Anchor knows about, and what was found of it."""

    name: str
    installed: bool
    policy_present: bool


@dataclass(frozen=True, slots=True)
class Facts:
    """Everything the checks are allowed to know."""

    session_active: bool = False

    blocker_last_seen_seconds: float | None = None
    """Seconds since ``anchor-blockerd`` last asked for the policy.

    ``None`` means it never has, which is how a blocker that is not running
    looks from inside the engine: they speak only when the blocker calls.
    """

    nftables_usable: bool = True
    table_loaded: bool = False
    upstreams: tuple[str, ...] = ()
    resolved_available: bool = False
    resolved_drop_in: bool = False
    browsers: tuple[BrowserFact, ...] = ()

    indicator: bool | None = None
    """Whether the panel extension is there. ``None`` means nobody could look."""


@dataclass(frozen=True, slots=True)
class Check:
    """One finding, and what to do about it."""

    name: str
    title: str
    ok: bool
    detail: str
    fix: str = ""
    blocking: bool = True
    """Whether failing means Anchor cannot do its job."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "ok": self.ok,
            "detail": self.detail,
            "fix": self.fix,
            "blocking": self.blocking,
        }


def examine(facts: Facts) -> list[Check]:
    """Every check, in the order a person should read them."""
    return [
        _blocker(facts),
        _dns(facts),
        _nftables(facts),
        _browsers(facts),
        _indicator(facts),
    ]


def worst(checks: list[Check]) -> str | None:
    """``problem``, ``note``, or nothing at all."""
    failed = [check for check in checks if not check.ok]
    if any(check.blocking for check in failed):
        return "problem"
    return "note" if failed else None


# -- the checks ----------------------------------------------------------


def _blocker(facts: Facts) -> Check:
    """Is anything actually blocking (SPEC 5.1)?"""
    title = "The blocker daemon"
    since = facts.blocker_last_seen_seconds

    if since is None:
        return Check(
            name="blocker",
            title=title,
            ok=False,
            detail=(
                "anchor-blockerd has never asked this engine for a policy, "
                "so nothing is being blocked."
            ),
            fix="sudo systemctl status anchor-blockerd",
        )
    if since > BLOCKER_SILENCE_SECONDS:
        return Check(
            name="blocker",
            title=title,
            ok=False,
            detail=(
                f"anchor-blockerd last checked in {format_duration(since)} ago. "
                "It polls once a second, so it has stopped."
            ),
            fix="sudo systemctl restart anchor-blockerd",
        )
    return Check(
        name="blocker",
        title=title,
        ok=True,
        detail=f"Checked in {format_duration(since)} ago.",
    )


def _dns(facts: Facts) -> Check:
    """Can queries be reached, and are they being (SPEC 8.2)?"""
    title = "The DNS path"

    if not facts.upstreams:
        return Check(
            name="dns",
            title=title,
            ok=False,
            detail=(
                "No upstream DNS server could be found, in systemd-resolved or in "
                "/etc/resolv.conf. Anchor will not redirect queries with nowhere to "
                "forward them, because that would stop every name on this machine "
                "from resolving — so blocking would not start."
            ),
            fix="Check this machine's network connection, then: resolvectl status",
        )

    found = ", ".join(facts.upstreams)

    if facts.session_active:
        if facts.resolved_available and not facts.resolved_drop_in:
            return Check(
                name="dns",
                title=title,
                ok=False,
                detail=(
                    "A session is running but systemd-resolved is not pointed at "
                    "Anchor's resolver. Queries can still be caught by the firewall "
                    "redirect, but the drop-in should be there."
                ),
                fix="sudo systemctl restart anchor-blockerd",
            )
        path = (
            "systemd-resolved is pointed at Anchor's resolver"
            if facts.resolved_available
            else (
                "no systemd-resolved here; upstreams come from /etc/resolv.conf, "
                f"and the resolver listens on port {RESOLVER_PORT}"
            )
        )
        return Check(name="dns", title=title, ok=True, detail=f"{path}. Upstream: {found}.")

    if facts.resolved_drop_in:
        return Check(
            name="dns",
            title=title,
            ok=False,
            detail=(
                "There is no session, but systemd-resolved is still pointed at "
                "Anchor's resolver. Something stopped without cleaning up."
            ),
            fix=RESTORE_FIX,
        )
    return Check(name="dns", title=title, ok=True, detail=f"Upstream: {found}.")


def _nftables(facts: Facts) -> Check:
    """Is the table where it should be, and only when it should be?"""
    title = "The firewall table"

    if not facts.nftables_usable:
        return Check(
            name="nftables",
            title=title,
            ok=False,
            detail="nftables is not usable here, so Anchor cannot redirect or block anything.",
            fix="Install nftables: sudo apt install nftables (or your distribution's package)",
        )

    if facts.session_active and not facts.table_loaded:
        return Check(
            name="nftables",
            title=title,
            ok=False,
            detail=(
                "A session is running but the inet anchor table is not loaded. "
                "DNS is not being redirected."
            ),
            fix="sudo systemctl restart anchor-blockerd",
        )

    if not facts.session_active and facts.table_loaded:
        return Check(
            name="nftables",
            title=title,
            ok=False,
            detail=(
                "No session is running, but Anchor's inet anchor table is still "
                "loaded. Something stopped without cleaning up, and this machine "
                "is being blocked by nothing."
            ),
            fix=RESTORE_FIX,
        )

    where = "loaded, as it should be during a session" if facts.table_loaded else "not loaded"
    return Check(name="nftables", title=title, ok=True, detail=f"inet anchor is {where}.")


def _browsers(facts: Facts) -> Check:
    """Are the installed browsers stopped from resolving on their own (SPEC 8.2)?"""
    title = "Browser policies"
    installed = [browser for browser in facts.browsers if browser.installed]

    if not installed:
        return Check(
            name="browsers",
            title=title,
            ok=True,
            detail="Anchor found no browser it knows how to configure.",
        )

    names = ", ".join(browser.name for browser in installed)
    missing = [browser.name for browser in installed if not browser.policy_present]
    present = [browser.name for browser in installed if browser.policy_present]

    if facts.session_active and missing:
        return Check(
            name="browsers",
            title=title,
            ok=False,
            detail=(
                f"A session is running, but DNS over HTTPS is not disabled in: "
                f"{', '.join(missing)}. Those browsers can resolve names by "
                "themselves and walk past the block."
            ),
            fix="sudo systemctl restart anchor-blockerd",
        )

    if not facts.session_active and present:
        return Check(
            name="browsers",
            title=title,
            ok=False,
            detail=(
                f"No session is running, but Anchor's managed policy is still in "
                f"place for: {', '.join(present)}. Their DNS settings are still "
                "locked, which is not Anchor's to keep between sessions."
            ),
            fix=RESTORE_FIX,
        )

    return Check(name="browsers", title=title, ok=True, detail=f"Found: {names}.")


def _indicator(facts: Facts) -> Check:
    """Is there anywhere for the top-bar item to appear (SPEC 14.1)?"""
    return indicator_check(facts.indicator)


def indicator_check(present: bool | None) -> Check:
    """The panel check, which only a client in the user's session can make.

    Public because that is exactly what happens: the engine answers ``None``
    and whichever client asked looks for itself and replaces this one, rather
    than the engine reporting on a bus it cannot reach.
    """
    title = "The top-bar indicator"

    if present is None:
        return Check(
            name="indicator",
            title=title,
            ok=True,
            detail=(
                "Could not be checked from here: the panel lives in the user's own "
                "session, and this engine runs outside it."
            ),
            blocking=False,
        )
    if not present:
        return Check(
            name="indicator",
            title=title,
            ok=False,
            detail=(
                "No StatusNotifierWatcher is running, so nothing will show Anchor's "
                "icon in the top bar. Everything else works: the time left is on the "
                "Home screen and in `anchor status`."
            ),
            fix=(
                "Install the AppIndicator extension: on Ubuntu it is already there; "
                "elsewhere, gnome-shell-extension-appindicator, then enable it."
            ),
            blocking=False,
        )
    return Check(
        name="indicator",
        title=title,
        ok=True,
        detail="A StatusNotifierWatcher is running.",
        blocking=False,
    )


# -- looking at the machine ----------------------------------------------


@dataclass
class Observer:
    """Everything :func:`gather` touches, in one place so tests can stand in.

    Not a convenience: it is what keeps the impure half small enough to read
    in one go, and small enough to be confident about without running it.
    """

    runner: Runner = run
    root: Path | None = None
    drop_in: Path = RESOLVED_DROP_IN
    resolver_port: int = RESOLVER_PORT
    browsers: tuple[Any, ...] = field(default=BROWSERS)


def gather(
    *,
    session_active: bool,
    blocker_last_seen: float | None,
    now: float | None = None,
    observer: Observer | None = None,
) -> Facts:
    """Look at the machine. Every call here can fail on a strange system."""
    look = observer or Observer()
    moment = time.time() if now is None else now

    return Facts(
        session_active=session_active,
        blocker_last_seen_seconds=(
            None if blocker_last_seen is None else max(0.0, moment - blocker_last_seen)
        ),
        nftables_usable=look.runner(["nft", "list", "ruleset"]).ok,
        table_loaded=rules_loaded(runner=look.runner),
        upstreams=tuple(_upstreams(look)),
        resolved_available=is_available(runner=look.runner),
        resolved_drop_in=look.drop_in.exists(),
        browsers=tuple(
            BrowserFact(
                name=browser.name,
                installed=browser.installed(look.root),
                policy_present=_policy_path(look, browser).exists(),
            )
            for browser in look.browsers
        ),
    )


def _upstreams(look: Observer) -> list[str]:
    try:
        return _discover_upstreams(runner=look.runner, resolver_port=look.resolver_port)
    except OSError:
        # A machine with no /etc/resolv.conf at all. Worth reporting as "none
        # found" rather than as a traceback the user has to interpret.
        return read_resolv_conf(None, resolver_port=look.resolver_port)


def _policy_path(look: Observer, browser: Any) -> Path:
    if look.root is None:
        path: Path = browser.policy_file
        return path
    return look.root / str(browser.policy_file).lstrip("/")
