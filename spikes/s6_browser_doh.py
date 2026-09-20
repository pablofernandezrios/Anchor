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
from typing import Any

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


RUNNING_WARNING = """
!!! Firefox is running right now, and this check cannot work while it is.

Firefox reads managed policies ONCE, when it starts. An instance that was
already running before the policy file existed will show an empty
about:policies whether or not the Snap can read /etc/firefox/policies, so the
result would tell you nothing.

Quit Firefox completely first: close every window, then confirm with

    pgrep -af firefox

and run this again. Opening a new tab or window is not enough, and neither is
`snap run firefox` while an instance is alive: that hands the request to the
process that is already there.
"""

SNAP_CHECK = """
The policy is now in place. To confirm the Snap Firefox actually reads it:

  1. START Firefox now, from cold. It must not have been running when this
     script wrote the policy, or it will not have read it.
  2. Go to          about:policies
  3. On the "Active" tab, look for:

         DNSOverHTTPS    {"Enabled": false, "Locked": true}

     If it is there, the Snap reads /etc/firefox/policies and SPEC 8.2 holds.
     If the Active tab is empty, or has no DNSOverHTTPS entry, it does not,
     and that is the answer we need.

  4. Check the "Errors" tab too. Anything there means the file was read but
     not understood, which is a different problem from not being read at all.

  5. Cross-check at about:preferences#privacy, under "Enable DNS over HTTPS":
     it should be off and greyed out, with a note that your organisation
     controls it.

Leave Firefox open while you look. Nothing is being blocked right now: this
writes the policy only, no firewall rules and no resolver.
"""


def firefox_processes() -> list[str]:
    """Any running Firefox, which would invalidate the check."""
    result = run("pgrep", "-af", "firefox")
    if not result.ok:
        return []
    return [
        line
        for line in result.out.splitlines()
        # The spike's own pgrep, and anything merely mentioning the word.
        if "firefox" in line.lower() and "pgrep" not in line
    ]


def snap_interfaces(report_lines: list[str]) -> None:
    """Show whether the Snap is even allowed to read /etc/firefox.

    The Ubuntu Firefox snap reaches the policy directory through a system-files
    interface. If that is not connected, the Snap cannot see the file however
    correct the file is, and that is a different answer from the Snap ignoring
    it.
    """
    result = run("snap", "connections", "firefox")
    if not result.ok:
        report_lines.append("  (snap connections unavailable)")
        return

    relevant = [
        line
        for line in result.out.splitlines()
        if "policies" in line or "system-files" in line or "etc-firefox" in line
    ]
    if relevant:
        report_lines.extend(f"  {line}" for line in relevant)
    else:
        report_lines.append(
            "  no policy-related interface is listed, which would explain a "
            "Snap that cannot read /etc/firefox/policies"
        )


def snap_check() -> int:
    """Write the Firefox policy, wait to be told, then take it away again.

    The ordinary spike restores every file before it exits, which is correct
    and makes this one check impossible: the policy is gone before a browser
    could read it. So this mode holds the file in place until you say you are
    done, and restores it even if you interrupt it.
    """
    if os.geteuid() != 0:
        print("this needs root: it writes /etc/firefox/policies/policies.json", file=sys.stderr)
        return 2

    running = firefox_processes()
    if running:
        print(RUNNING_WARNING)
        for line in running[:5]:
            print(f"    {line}")
        return 1

    interfaces: list[str] = []
    snap_interfaces(interfaces)
    if interfaces:
        print("Snap interfaces that bear on this:")
        print("\n".join(interfaces))
        print()

    target = Path("/etc/firefox/policies/policies.json")
    existed = target.exists()
    original = target.read_text(encoding="utf-8") if existed else None

    try:
        merged: dict[str, Any] = {}
        if original:
            try:
                loaded = json.loads(original)
                if isinstance(loaded, dict):
                    merged = loaded
            except json.JSONDecodeError:
                print("note: the existing policy file is not valid JSON; writing a fresh one")

        policies = merged.get("policies")
        if not isinstance(policies, dict):
            policies = {}
        policies.update(_policy_for("firefox")["policies"])  # type: ignore[index]
        merged["policies"] = policies

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(merged, indent=2) + "\\n", encoding="utf-8")

        print(SNAP_CHECK)
        input("Press Enter when you have looked, and the policy will be removed: ")
    finally:
        if existed and original is not None:
            target.write_text(original, encoding="utf-8")
            print(f"restored {target} as it was")
        else:
            target.unlink(missing_ok=True)
            for parent in (target.parent, target.parent.parent):
                try:
                    if parent.is_dir() and not any(parent.iterdir()):
                        parent.rmdir()
                except OSError:
                    break
            print(f"removed {target}")

    return 0


if __name__ == "__main__":
    if "--snap-check" in sys.argv:
        raise SystemExit(snap_check())

    raise SystemExit(
        main(
            spike,
            SpikeReport(
                spike="s6-browser-doh",
                question="Which policy path does each browser really read?",
            ),
        )
    )
