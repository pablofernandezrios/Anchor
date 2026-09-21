"""What Anchor tells the user, and when (SPEC 7.1, 8.3, 9).

Three moments, all of them specified, and nothing else. A focus tool that
chats is a focus tool people turn off, so the agent says something only when
the specification says it must:

* a site was blocked (SPEC 8.3),
* applications are about to close, with time to save (SPEC 7.1),
* an application was closed (SPEC 9).

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


class Urgency(IntEnum):
    """The freedesktop notification urgencies."""

    LOW = 0
    NORMAL = 1
    CRITICAL = 2


#: Notifications in one channel replace each other rather than stacking, so a
#: burst of blocked sites leaves one notification and not fifteen.
BLOCKED_CHANNEL = "blocked-site"
APPS_CHANNEL = "applications"

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
