"""First run: what Anchor is, and proof that it works (SPEC 14).

SPEC 14 asks for an introduction that explains levels, the valve and
allowlists, and ends "with a live test that blocks a sample domain and
verifies it". The explaining is words. The test is the interesting part,
because it is the one moment where Anchor demonstrates rather than promises.

**How the test ends is the design decision.** It starts a real session, with
real blocking, and then has to stop. A Strict session could not be stopped at
all, and a Soft one cannot be cancelled for five minutes (SPEC 7.2) — so the
test uses a Soft session *twenty seconds long* and lets it expire. No exit
that skips the friction was added, and none should ever be: an "end this one
early" request would be the single hole that makes every other guarantee in
Anchor negotiable. Twenty seconds of a reserved domain being unreachable is a
small price for a promise nobody has to take on trust.

**The sample is ``example.com``**, reserved by IANA for exactly this kind of
use, with ``example.org`` as the control. Blocking a domain the user actually
visits to prove a point would be a strange way to introduce a tool about
self-control, and a test with no control row cannot tell "Anchor blocked it"
from "this machine has no network" — a distinction the leak tests learned to
make the hard way.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass
from typing import Any, Final

from anchor.gui.i18n import _

#: Reserved by IANA, resolvable everywhere, nobody's daily reading.
SAMPLE_DOMAIN: Final = "example.com"
CONTROL_DOMAIN: Final = "example.org"

#: The profile the test makes for itself, and takes away afterwards.
TEST_PROFILE: Final = "Anchor test"

#: How long the test session runs. It ends by expiring, because no way to end
#: one early exists — see the module docstring.
TEST_SECONDS: Final = 20

#: How long to wait for a name to resolve before calling it unreachable.
RESOLVE_TIMEOUT: Final = 3.0


@dataclass(frozen=True, slots=True)
class Step:
    """One page of the introduction."""

    key: str
    title: str
    body: str = ""
    points: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TestResult:
    """What the live test showed."""

    ok: bool
    failed: bool
    """Whether it actually went wrong, as opposed to proving nothing."""

    title: str
    detail: str
    fix: str = ""


def _steps() -> tuple[Step, ...]:
    return (
        Step(
            key="welcome",
            title=_("Anchor"),
            body=_(
                "Anchor makes it harder to reach what pulls you away, for as long "
                "as you asked. It is friction, not a lock: everything here is "
                "something you chose, and nothing pretends to be unbreakable."
            ),
        ),
        Step(
            key="levels",
            title=_("Three levels of difficulty"),
            body=_("You pick one when you start a session. They differ in how you get out."),
            points=(
                _("Soft: cancel after waiting 5 minutes. Your lists stay editable."),
                _("Firm: cancel after a long wait and typing a long random text."),
                _(
                    "Strict: it cannot be cancelled. The emergency valve is the only "
                    "way out, and VPN and Tor are blocked for the session."
                ),
            ),
        ),
        Step(
            key="valve",
            title=_("The emergency valve"),
            body=_(
                "A Strict session still has a way out, chosen before it starts so "
                "that you cannot bargain with yourself later."
            ),
            points=(
                _("A 30-minute wait, which you can withdraw if you change your mind."),
                _("A long random phrase, generated fresh each time and typed by hand."),
                _("Both, one after the other."),
                _(
                    "Every use is recorded as a rupture in your statistics. Nothing "
                    "is hidden from you, and nothing is sent anywhere."
                ),
            ),
        ),
        Step(
            key="allowlist",
            title=_("Blocklists and allowlists"),
            body=_(
                "A profile either blocks what you list, or blocks everything except "
                "what you list."
            ),
            points=(
                _("A blocklist is the usual choice, and the gentler one."),
                _(
                    "An allowlist blocks all of the web except your list. Many sites "
                    "load parts of themselves from other domains and will need "
                    "entries of their own."
                ),
                _(
                    "Anchor always allows a small built-in list — connectivity checks "
                    "and time sync — so the machine keeps working."
                ),
            ),
        ),
        Step(
            key="test",
            title=_("Let us prove it works"),
            body=_(
                "Anchor will block {sample} for {seconds} seconds and check that it "
                "really stopped resolving, while {control} keeps working. The test "
                "session ends by itself: Anchor has no way to end a session early, "
                "which is rather the point."
            ).format(sample=SAMPLE_DOMAIN, seconds=TEST_SECONDS, control=CONTROL_DOMAIN),
        ),
    )


STEPS: Final = _steps()


def indicator_step(present: bool | None) -> Step:
    """What to say about the top bar (SPEC 14.1).

    ``None`` means the check could not be made, which is not the same as the
    extension being missing and must not be reported as though it were.
    """
    if present is None:
        return Step(
            key="indicator",
            title=_("The top bar"),
            points=(
                _(
                    "Anchor could not check whether your desktop can show a tray "
                    "icon. The time left is on the Home screen either way."
                ),
            ),
        )
    if present:
        return Step(
            key="indicator",
            title=_("The top bar"),
            points=(
                _("Your desktop can show Anchor's icon and the time left while a session runs."),
            ),
        )
    return Step(
        key="indicator",
        title=_("The top bar"),
        points=(
            _("Nothing on this desktop can show a tray icon, so Anchor's will not appear."),
            _(
                "On GNOME, install the AppIndicator extension "
                "(gnome-shell-extension-appindicator) and enable it. Ubuntu already "
                "has it."
            ),
            _("Everything else works. The time left is on Home and in `anchor status`."),
        ),
    )


# -- the live test -------------------------------------------------------


def setup_requests() -> list[tuple[str, dict[str, Any]]]:
    """Build the test's own profile and start its short session."""
    return [
        (
            "profile.create",
            {
                "name": TEST_PROFILE,
                "web_mode": "blocklist",
                "domains": [SAMPLE_DOMAIN],
                "apps": [],
                "categories": [],
                # Long enough that no break can interrupt a session this short.
                "breaks": {"work_minutes": 60, "break_minutes": 5},
            },
        ),
        (
            "session.start",
            {
                "profile": TEST_PROFILE,
                "duration_seconds": TEST_SECONDS,
                "level": "soft",
            },
        ),
    ]


def cleanup_requests() -> list[tuple[str, dict[str, Any]]]:
    """Take the test's profile away again once its session has ended."""
    return [("profile.delete", {"name": TEST_PROFILE})]


def finished_request() -> tuple[str, dict[str, Any]]:
    """Remember that the introduction has been through (SPEC 14)."""
    return "config.set", {"key": "onboarding_done", "value": "yes"}


def resolves(name: str, *, timeout: float = RESOLVE_TIMEOUT) -> bool:
    """Whether a name answers. The impure half of the test."""
    previous = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout)
    try:
        socket.getaddrinfo(name, None)
    except (OSError, socket.gaierror):
        return False
    finally:
        socket.setdefaulttimeout(previous)
    return True


def verdict_for(*, sample_resolves: bool, control_resolves: bool) -> TestResult:
    """What the two lookups mean.

    The control is what makes this a test rather than a coincidence: a machine
    with no network resolves nothing, which looks exactly like perfect
    blocking until you ask a second question.
    """
    if sample_resolves:
        return TestResult(
            ok=False,
            failed=True,
            title=_("The block did not take effect"),
            detail=_(
                "{sample} still resolved while Anchor was blocking it, so something "
                "is in the way of the DNS path."
            ).format(sample=SAMPLE_DOMAIN),
            fix=_(
                "Run `anchor doctor`: it checks the resolver, the firewall table and "
                "the browser policies, and prints what to do."
            ),
        )

    if not control_resolves:
        return TestResult(
            ok=False,
            failed=False,
            title=_("The test could not tell"),
            detail=_(
                "Neither {sample} nor {control} resolved, so this machine's network "
                "is down and the test proves nothing either way. Try it again once "
                "you are connected."
            ).format(sample=SAMPLE_DOMAIN, control=CONTROL_DOMAIN),
            fix=_("Settings has a way to run this again."),
        )

    return TestResult(
        ok=True,
        failed=False,
        title=_("Anchor is working"),
        detail=_(
            "{sample} stopped resolving while the session ran, and {control} kept "
            "working. That is exactly what a session does to the sites you block."
        ).format(sample=SAMPLE_DOMAIN, control=CONTROL_DOMAIN),
    )
