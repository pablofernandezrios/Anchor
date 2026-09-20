"""Deciding whether a name is blocked (SPEC 8.1).

A rule is a domain and it covers every subdomain, so ``example.com`` catches
``www.example.com`` but not ``notexample.com``. Matching is on label
boundaries for that reason: a substring test would let
``bbc.co.uk.somewhere-else.com`` masquerade as the site it names.

Two things are never blocked, whatever the lists say. Special-use names such
as ``localhost`` and ``.arpa`` belong to the machine rather than the web, and
blocking them breaks the computer instead of a distraction. And the essentials
list keeps connectivity checks and time synchronisation working, which matters
most in allowlist mode, where anything unnamed is blocked by default.
"""

from __future__ import annotations

import logging
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

from anchor.protocol.types import WebMode

log = logging.getLogger("anchor-blockerd")

#: Names that belong to the machine, not to the web (RFC 6761, RFC 6762).
#:
#: Most never reach Anchor at all: systemd-resolved answers them itself without
#: forwarding, which is what Milestone 0 spike 1 discovered the hard way. They
#: are listed anyway, because a distribution without systemd-resolved sends
#: them straight through.
SPECIAL_USE: Final[frozenset[str]] = frozenset(
    {
        "localhost",
        "local",
        "arpa",
        "home.arpa",
        "localdomain",
    }
)


class NotADomainError(ValueError):
    """The text is not something Anchor can turn into a rule."""


def normalise_domain(raw: str) -> str:
    """Turn what a person typed into a domain.

    People paste whole URLs into a blocklist, so a scheme, a path, a port and a
    trailing dot are all stripped rather than refused. International names are
    converted to punycode, because that is what arrives on the wire.
    """
    text = raw.strip().lower()
    if not text:
        raise NotADomainError(f"{raw!r} is not a domain")

    if "//" in text:
        text = urlsplit(text).netloc or urlsplit(text).path

    text = text.split("/", 1)[0]
    text = text.rsplit("@", 1)[-1]

    # Strip a port, but leave an IPv6 literal's colons alone.
    if text.count(":") == 1:
        text = text.split(":", 1)[0]

    text = text.strip(".")
    if not text:
        raise NotADomainError(f"{raw!r} is not a domain")

    # Convert an international name to the punycode that arrives on the wire.
    # A name the codec will not touch is already ASCII, or is unusable, and
    # the check below catches the second case.
    with suppress(UnicodeError):
        text = text.encode("idna").decode("ascii")

    text = text.strip(".").lower()
    if not text:
        raise NotADomainError(f"{raw!r} is not a domain")
    return text


def _covers(rule: str, name: str) -> bool:
    """Whether ``rule`` covers ``name``, on label boundaries."""
    return name == rule or name.endswith("." + rule)


@dataclass(frozen=True, slots=True)
class Policy:
    """What one session blocks."""

    mode: WebMode
    domains: frozenset[str]
    essentials: frozenset[str] = frozenset()

    def matching_rule(self, name: str) -> str | None:
        """The rule that blocks ``name``, or ``None`` if it is allowed.

        Named rather than merely true or false because the statistics record
        which rule caught a name (SPEC 13), and because the notification tells
        the user what they hit.
        """
        candidate = name.strip().lower().rstrip(".")

        if self._is_exempt(candidate):
            return None

        matches = [rule for rule in self.domains if _covers(rule, candidate)]

        if self.mode is WebMode.BLOCKLIST:
            if not matches:
                return None
            # The longest rule is the most specific one.
            return max(matches, key=len)

        # Allowlist: anything the list does not cover is blocked, and the rule
        # responsible is the mode itself rather than any one entry.
        return None if matches else "allowlist"

    def is_blocked(self, name: str) -> bool:
        return self.matching_rule(name) is not None

    def _is_exempt(self, name: str) -> bool:
        if not name:
            # The root, and anything Anchor could not read a name from. Not a
            # site, so not something to block.
            return True
        if any(_covers(suffix, name) for suffix in SPECIAL_USE):
            return True
        return any(_covers(rule, name) for rule in self.essentials)


def load_domain_file(path: Path) -> frozenset[str]:
    """Read a domain list such as ``essentials.txt`` (SPEC 8.1, 12).

    Blank lines and ``#`` comments are ignored, and a line that is not a domain
    is skipped with a warning rather than taken down the whole file: a typo in
    a shipped data file should cost one entry, not the list.
    """
    domains: set[str] = set()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        log.warning("could not read the domain list at %s: %s", path, error)
        return frozenset()

    for number, line in enumerate(text.splitlines(), start=1):
        entry = line.split("#", 1)[0].strip()
        if not entry:
            continue
        try:
            domains.add(normalise_domain(entry))
        except NotADomainError:
            log.warning("%s line %d: %r is not a domain; skipping", path, number, entry)

    return frozenset(domains)
