"""What Anchor tells the user, and when (SPEC 7.1, 8.3, 9).

A few moments, all of them specified, and nothing else. A focus tool that
chats is a focus tool people turn off, so the agent says something only when
the specification says it must:

* a site was blocked (SPEC 8.3),
* applications are about to close, with time to save (SPEC 7.1),
* an application was closed (SPEC 9),
* a break is coming, and a break has begun (SPEC 10, ADR 2).

The ten-minute limit SPEC 8.3 asks for is not applied here. It lives in the
blocker, which stops counting a domain it has already reported, so by the time
an event reaches the agent the limit has already been honoured. Applying it
twice would mean two different ideas of what the user has seen, and the one in
the agent would be lost every time the user logged out.

The text here is English. SPEC 14 asks for Spanish as well, through gettext,
which arrives with the interface it was written for.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

from anchor.protocol.types import BreakType


class Urgency(IntEnum):
    """The freedesktop notification urgencies."""

    LOW = 0
    NORMAL = 1
    CRITICAL = 2


#: Notifications in one channel replace each other rather than stacking, so a
#: burst of blocked sites leaves one notification and not fifteen.
BLOCKED_CHANNEL = "blocked-site"
APPS_CHANNEL = "applications"
BREAK_CHANNEL = "break"

#: Let the notification server choose how long to show it.
SERVER_DEFAULT_TIMEOUT = -1


@dataclass(frozen=True, slots=True)
class Notification:
    """One thing to say, and how to say it."""

    summary: str
    body: str = ""
    icon: str = "alarm-symbolic"
    urgency: Urgency = Urgency.NORMAL
    channel: str = ""
    timeout_ms: int = SERVER_DEFAULT_TIMEOUT


def plan(event: str, payload: dict[str, Any]) -> Notification | None:
    """What to show for an engine event, or ``None`` to stay quiet."""
    match event:
        case "blocked.attempt":
            return _blocked(payload)
        case "apps.grace":
            return _grace(payload)
        case "apps.closed":
            return _closed(payload)
        case "break.warning":
            return _break_coming(payload)
        case "break.started":
            return _break_started(payload)
        case "break.ended":
            return _break_ended(payload)
    return None


def _blocked(payload: dict[str, Any]) -> Notification | None:
    """SPEC 8.3: the browser shows its own error; this says who did it."""
    domain = str(payload.get("domain", "")).strip()
    if not domain:
        return None

    rule = str(payload.get("rule", "")).strip()
    body = f"{domain} is blocked during this session."
    if rule and rule != domain:
        # Worth saying: the user blocked youtube.com and is looking at a
        # refusal for m.youtube.com, which is not obviously the same thing.
        body = f"{domain} is blocked during this session, by the rule {rule}."

    return Notification(
        summary="Site blocked",
        body=body,
        icon="dialog-information-symbolic",
        channel=BLOCKED_CHANNEL,
    )


def _grace(payload: dict[str, Any]) -> Notification | None:
    """SPEC 7.1: two minutes to save, and the warning that makes them useful."""
    names = _names(payload)
    if not names:
        return None

    seconds = int(payload.get("seconds", 0))
    when = _in_words(seconds)
    subject = "These applications" if len(names) > 1 else "This application"

    return Notification(
        summary=f"{subject} will close {when}",
        body=f"{_join(names)}. Save your work now.",
        icon="document-save-symbolic",
        # It is about losing unsaved work, and it has a deadline. This is the
        # one notification Anchor sends that must not slide past unseen.
        urgency=Urgency.CRITICAL,
        channel=APPS_CHANNEL,
        # Gone exactly when the applications go, rather than lingering to warn
        # about something that has already happened.
        timeout_ms=max(1, seconds) * 1000 if seconds else SERVER_DEFAULT_TIMEOUT,
    )


def _closed(payload: dict[str, Any]) -> Notification | None:
    """SPEC 9: an application was closed, and the user is told which."""
    names = _names(payload)
    if not names:
        return None

    launched = str(payload.get("reason", "")) == "launch"
    body = (
        "It is blocked during this session."
        if len(names) == 1
        else "They are blocked during this session."
    )
    summary = f"Anchor closed {_join(names)}"
    if launched:
        summary = f"{_join(names)} is blocked" if len(names) == 1 else f"{_join(names)} are blocked"
        body = "Anchor closed it as it opened." if len(names) == 1 else "Anchor closed them."

    return Notification(
        summary=summary,
        body=body,
        icon="window-close-symbolic",
        channel=APPS_CHANNEL,
    )


def _names(payload: dict[str, Any]) -> list[str]:
    raw = payload.get("apps") or []
    if not isinstance(raw, list):
        return []
    return [str(name).strip() for name in raw if str(name).strip()]


def _join(names: list[str]) -> str:
    """``Discord``, ``Discord and Slack``, ``Discord, Slack and Steam``."""
    if len(names) == 1:
        return names[0]
    return f"{', '.join(names[:-1])} and {names[-1]}"


def _in_words(seconds: int) -> str:
    if seconds <= 0:
        return "now"
    if seconds < 60:
        return f"in {seconds} seconds"
    minutes = math.ceil(seconds / 60)
    return "in a minute" if minutes == 1 else f"in {minutes} minutes"


def _break_coming(payload: dict[str, Any]) -> Notification | None:
    """ADR 2: a break must never arrive unannounced.

    This is the whole of what Anchor promises about a Mandatory break. It
    cannot hold the screen on Wayland, so it makes sure the break is never a
    surprise; that promise is only kept if this notification is reliable.
    """
    seconds = int(float(payload.get("seconds", 0)))
    length = int(float(payload.get("break_seconds", 0)))
    body = f"It lasts {_minutes(length)}." if length else ""
    return Notification(
        summary=f"Break {_in_words(seconds)}",
        body=body,
        icon="alarm-symbolic",
        channel=BREAK_CHANNEL,
        # Gone by the time the break starts, so it never sits next to the
        # alert saying the break has already begun.
        timeout_ms=max(1, seconds) * 1000 if seconds else SERVER_DEFAULT_TIMEOUT,
    )


def _break_started(payload: dict[str, Any]) -> Notification | None:
    """ADR 2: an alert when it begins, alongside the overlay if there is one."""
    seconds = int(float(payload.get("seconds", 0)))
    long = bool(payload.get("long"))
    return Notification(
        summary="Long break" if long else "Break time",
        body=f"Take {_minutes(seconds)}." if seconds else "",
        icon="media-playback-pause-symbolic",
        channel=BREAK_CHANNEL,
    )


def _break_ended(payload: dict[str, Any]) -> Notification | None:
    """Only when nothing else would say so.

    A break the user skipped or postponed needs no announcement: they are the
    one who ended it. An overlay says it is over by disappearing. A break that
    was only ever a notification has no other signal, and a break with no end
    is not a break, so that one is announced.
    """
    if payload.get("skipped") or payload.get("postponed"):
        return None
    if str(payload.get("type", "")) != str(BreakType.NOTIFICATION):
        return None

    return Notification(
        summary="Break over",
        body="Back to work.",
        icon="alarm-symbolic",
        channel=BREAK_CHANNEL,
    )


def _minutes(seconds: int) -> str:
    minutes = math.ceil(seconds / 60)
    if minutes <= 1:
        return "a minute"
    return f"{minutes} minutes"
