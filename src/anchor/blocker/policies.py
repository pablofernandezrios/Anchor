"""Turning off DNS-over-HTTPS in the browsers on this machine (SPEC 8.2).

A browser that resolves names itself over HTTPS never asks the system resolver,
so it walks straight past Anchor. Every supported browser can be told not to,
through the managed-policy mechanism its vendor provides. Milestone 0 spike 6
confirmed the paths on Ubuntu.

Two things are worth knowing about the limits of this.

**Policies apply when a browser starts.** A browser already running when a
session begins keeps whatever setting it had. That gap is covered by the
firewall rules, which reject the known DoH endpoints outright, so an unrestarted
browser fails its DoH connection and falls back to system DNS, where Anchor is
waiting.

**Firefox has one policy file and other things write to it.** So Firefox's file
is merged rather than replaced: Anchor adds its key and leaves every other
policy alone. The Chromium family reads a directory of files, so Anchor writes
its own and touches nothing else.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from anchor.blocker.journal import Journal

log = logging.getLogger("anchor-blockerd")

#: Firefox: one file, shared with anything else that manages Firefox.
FIREFOX_POLICY: Final[dict[str, Any]] = {"DNSOverHTTPS": {"Enabled": False, "Locked": True}}

#: The Chromium family: a directory of files, one of which can be ours.
#: ``BuiltInDnsClientEnabled`` matters as much as the mode: with the built-in
#: resolver on, Chrome can bypass the system stub even without DoH.
CHROMIUM_POLICY: Final[dict[str, Any]] = {
    "DnsOverHttpsMode": "off",
    "BuiltInDnsClientEnabled": False,
}


@dataclass(frozen=True, slots=True)
class Browser:
    """One browser, where to find it, and where its policy goes."""

    name: str
    detect: tuple[str, ...]
    policy_file: Path
    merges: bool
    """Whether the file is shared and must be merged rather than replaced."""

    def installed(self, root: Path | None = None) -> bool:
        return any(_under(root, candidate).exists() for candidate in self.detect)


def _under(root: Path | None, path: str | Path) -> Path:
    """Resolve a path, optionally inside a test root."""
    if root is None:
        return Path(path)
    return root / str(path).lstrip("/")


#: Every browser Anchor knows how to configure. The Firefox entries share a
#: policy file: the deb and the Snap both read /etc/firefox/policies, which is
#: the assumption spike 6 flagged for confirmation inside the Snap.
BROWSERS: Final[tuple[Browser, ...]] = (
    Browser(
        name="Firefox",
        detect=(
            "/usr/bin/firefox",
            "/usr/lib/firefox/firefox",
            "/snap/firefox/current/usr/lib/firefox/firefox",
        ),
        policy_file=Path("/etc/firefox/policies/policies.json"),
        merges=True,
    ),
    Browser(
        name="Google Chrome",
        detect=("/opt/google/chrome/chrome", "/usr/bin/google-chrome"),
        policy_file=Path("/etc/opt/chrome/policies/managed/anchor.json"),
        merges=False,
    ),
    Browser(
        name="Chromium",
        detect=(
            "/usr/bin/chromium",
            "/usr/lib/chromium/chromium",
            "/snap/chromium/current/usr/lib/chromium-browser/chrome",
        ),
        policy_file=Path("/etc/chromium/policies/managed/anchor.json"),
        merges=False,
    ),
    Browser(
        name="Brave",
        detect=("/opt/brave.com/brave/brave", "/usr/bin/brave-browser"),
        policy_file=Path("/etc/brave/policies/managed/anchor.json"),
        merges=False,
    ),
    Browser(
        name="Microsoft Edge",
        detect=("/opt/microsoft/msedge/msedge", "/usr/bin/microsoft-edge"),
        policy_file=Path("/etc/opt/edge/policies/managed/anchor.json"),
        merges=False,
    ),
)


def _merge_firefox(existing: str | None) -> str:
    """Add Anchor's key to a Firefox policy file, keeping everything else."""
    document: dict[str, Any] = {}
    if existing:
        try:
            loaded = json.loads(existing)
            if isinstance(loaded, dict):
                document = loaded
        except json.JSONDecodeError:
            # Something already wrote rubbish there. Replacing it would lose
            # whatever it meant, so start clean and say so.
            log.warning(
                "the existing Firefox policy file is not valid JSON; "
                "writing a fresh one and keeping the original for restoration"
            )

    policies = document.get("policies")
    if not isinstance(policies, dict):
        policies = {}
    policies.update(FIREFOX_POLICY)
    document["policies"] = policies
    return json.dumps(document, indent=2) + "\n"


def apply_policies(journal: Journal, *, root: Path | None = None) -> list[str]:
    """Disable DoH in every installed browser. Returns the ones configured."""
    configured: list[str] = []

    for browser in BROWSERS:
        if not browser.installed(root):
            continue

        target = _under(root, browser.policy_file)
        try:
            if browser.merges:
                existing = target.read_text(encoding="utf-8") if target.exists() else None
                content = _merge_firefox(existing)
            else:
                content = json.dumps(CHROMIUM_POLICY, indent=2) + "\n"

            journal.write_file(target, content)
        except OSError as error:
            log.warning("could not write the %s policy at %s: %s", browser.name, target, error)
            continue

        configured.append(browser.name)

    if configured:
        log.info(
            "DNS-over-HTTPS disabled for %s. A browser already running keeps its "
            "old setting until it restarts; the firewall rules cover that gap",
            ", ".join(configured),
        )
    else:
        log.info("no supported browser found, so no policies were written")

    return configured
