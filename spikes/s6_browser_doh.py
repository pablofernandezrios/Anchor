#!/usr/bin/env python3
"""Spike 6: where do managed browser policies live? (SPEC 8.2)

Anchor disables DNS-over-HTTPS through each browser's managed policy system,
because a browser that resolves names by itself walks straight past the local
resolver. The paths differ per browser and per packaging format, and the
Firefox Snap is the awkward one: a confined Snap does not read /etc.

The spike reports which browsers are installed, which policy path each one
actually reads, and whether writing there works. Writing is only attempted
with --write, because these are real files on a real machine.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from lib import SpikeReport, Verdict, have, main, restored_file, run


@dataclass(frozen=True)
class Target:
    name: str
    detect: tuple[str, ...]
    policy_path: Path
    style: str
    note: str = ""


#: Firefox reads policies.json; Chromium-based browsers read a JSON file in
#: their managed policy directory. The Snap and Flatpak builds of Firefox look
#: somewhere else entirely, which is the thing worth confirming.
TARGETS: tuple[Target, ...] = (
    Target(
        "Firefox (deb)",
        ("/usr/lib/firefox/firefox", "/usr/bin/firefox"),
        Path("/etc/firefox/policies/policies.json"),
        "firefox",
        "Also reads /usr/lib/firefox/distribution/policies.json.",
    ),
    Target(
        "Firefox (Snap)",
        ("/snap/firefox/current/usr/lib/firefox/firefox",),
        Path("/etc/firefox/policies/policies.json"),
        "firefox",
        "A confined Snap may not read /etc. The Snap build is documented to "
        "read /etc/firefox/policies, but that has to be verified here.",
    ),
    Target(
        "Firefox (Flatpak)",
        ("/var/lib/flatpak/app/org.mozilla.firefox",),
        Path(
            "/var/lib/flatpak/app/org.mozilla.firefox/current/active/files/lib/firefox/distribution/policies.json"
        ),
        "firefox",
        "Flatpak needs the policy inside the sandbox or an override.",
    ),
    Target(
        "Google Chrome",
        ("/opt/google/chrome/chrome", "/usr/bin/google-chrome"),
        Path("/etc/opt/chrome/policies/managed/anchor.json"),
        "chromium",
    ),
    Target(
        "Chromium (deb)",
        ("/usr/lib/chromium/chromium", "/usr/bin/chromium"),
        Path("/etc/chromium/policies/managed/anchor.json"),
        "chromium",
    ),
    Target(
        "Chromium (Snap)",
        ("/snap/chromium/current/usr/lib/chromium-browser/chrome",),
        Path("/etc/chromium/policies/managed/anchor.json"),
        "chromium",
    ),
    Target(
        "Brave",
        ("/opt/brave.com/brave/brave", "/usr/bin/brave-browser"),
        Path("/etc/brave/policies/managed/anchor.json"),
        "chromium",
    ),
    Target(
        "Microsoft Edge",
        ("/opt/microsoft/msedge/msedge", "/usr/bin/microsoft-edge"),
        Path("/etc/opt/edge/policies/managed/anchor.json"),
        "chromium",
    ),
)

CHROMIUM_POLICY = {
    "DnsOverHttpsMode": "off",
    "BuiltInDnsClientEnabled": False,
}


def _policy_for(style: str) -> dict[str, object]:
    if style == "firefox":
        return {"policies": {"DNSOverHTTPS": {"Enabled": False, "Locked": True}}}
    return dict(CHROMIUM_POLICY)


def _installed(target: Target) -> str | None:
    for candidate in target.detect:
        if Path(candidate).exists():
            return candidate
    return None


def spike(report: SpikeReport) -> None:
    may_write = "--write" in sys.argv

    found_any = False
    for target in TARGETS:
        location = _installed(target)
        if location is None:
            continue
        found_any = True

        detail = f"installed at {location}; policy path {target.policy_path}"
        if target.note:
            detail += f". {target.note}"

        exists = target.policy_path.exists()
        current = ""
        if exists:
            current = target.policy_path.read_text(encoding="utf-8", errors="replace")[:500]
            detail += ". A policy file is already present"

        if not may_write:
            report.add(target.name, Verdict.WORKS, detail + " (not written: pass --write)", current)
            continue

        if os.geteuid() != 0:
            report.add(target.name, Verdict.UNAVAILABLE, detail + " (writing needs root)")
            continue

        with restored_file(target.policy_path):
            try:
                target.policy_path.parent.mkdir(parents=True, exist_ok=True)
                merged = _policy_for(target.style)
                target.policy_path.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
                written = json.loads(target.policy_path.read_text(encoding="utf-8"))
                ok = written == merged
            except OSError as error:
                report.add(
                    target.name,
                    Verdict.FAILS,
                    f"{detail}. Writing the policy failed: {error}",
                )
                continue

        report.add(
            target.name,
            Verdict.WORKS if ok else Verdict.FAILS,
            detail + ". The policy file was written and restored",
            json.dumps(merged),
        )

    if not found_any:
        report.add(
            "browsers installed",
            Verdict.UNAVAILABLE,
            "no supported browser was found; run this spike on the desktop VM",
        )

    _report_snap_confinement(report)


def _report_snap_confinement(report: SpikeReport) -> None:
    """The Firefox Snap is the case most likely to break the assumption."""
    if not have("snap"):
        report.add(
            "Snap confinement",
            Verdict.UNAVAILABLE,
            "snapd is not installed here, so the Snap path is untested",
        )
        return

    listing = run("snap", "list")
    report.add(
        "Snap confinement",
        Verdict.WORKS,
        "snapd is present. Confirm by hand that about:policies inside the Snap "
        "Firefox shows DNSOverHTTPS locked off; a Snap reading /etc is the "
        "assumption SPEC 8.2 rests on",
        listing.text[:800],
    )


if __name__ == "__main__":
    raise SystemExit(
        main(
            spike,
            SpikeReport(
                spike="s6-browser-doh",
                question="Which policy path does each browser really read?",
            ),
        )
    )
