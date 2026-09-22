"""What a screen says when it has no answer to draw (SPEC 14).

Every screen but Home asked the engine, and on a failure showed a toast and
nothing else: a blank page under a message that disappears after six seconds.
The first run on a real desktop found it at once — the engine was not running,
Home degraded correctly, and the other five pages were empty rectangles.

A page with nothing on it is the one state a user cannot act on. It does not
say whether Anchor is broken, still loading, or simply has nothing to show, so
this turns every failure into a sentence and, where there is one, a way out.
"""

from __future__ import annotations

from dataclasses import dataclass

from anchor.gui.i18n import _

#: What the engine answers when the socket is not there at all.
UNREACHABLE = "UNREACHABLE"


@dataclass(frozen=True, slots=True)
class Outage:
    """A screen's empty state: a title, a sentence, and sometimes a remedy."""

    title: str
    detail: str
    remedy: str = ""


def outage(code: str, message: str = "") -> Outage:
    """Turn a failed reply into something a person can read and act on.

    The engine being down is its own case, because it is the only one with a
    remedy the user can carry out, and because it is what a machine looks like
    before `anchord` has been installed or started at all.
    """
    if code == UNREACHABLE:
        return Outage(
            title=_("Anchor is not running"),
            detail=_(
                "The engine is what decides and records everything, so there is "
                "nothing to show until it is running."
            ),
            remedy=_("Start it with: systemctl start anchord"),
        )

    return Outage(
        title=_("This could not be loaded"),
        # The engine's own words, which say more than anything invented here.
        detail=message or _("Anchor asked its engine and got no usable answer."),
    )
