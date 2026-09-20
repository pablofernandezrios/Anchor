#!/usr/bin/env python3
"""Enforce the coverage target for engine logic (SPEC 18).

The 80% target applies to the modules the specification names: sessions,
ratchet, schedules, breaks and time. The rest of the tree is not gated, so a
thin client without tests cannot mask an untested engine.
"""

from __future__ import annotations

import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

TARGET = 80.0

# Modules the specification puts under the coverage target. Coverage reports
# paths relative to the project root, which may or may not carry the src/
# prefix depending on how it was invoked, so both spellings are accepted.
GATED_SUFFIXES = (
    "anchor/engine/",
    "anchor/protocol/",
    "anchor/blocker/",
)


def _is_gated(filename: str) -> bool:
    normalised = filename.replace("\\", "/").removeprefix("./").removeprefix("src/")
    return normalised.startswith(GATED_SUFFIXES)


def main() -> int:
    # Always regenerate from the current .coverage. Reusing whatever
    # coverage.xml happens to be lying around lets a stale report vouch for
    # code that was never measured, which is the one thing this must not do.
    report = Path("coverage.xml")
    result = subprocess.run(
        [sys.executable, "-m", "coverage", "xml", "-o", str(report)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print(f"FAILED: could not generate the coverage report: {result.stderr.strip()}")
        return 1

    root = ET.parse(report).getroot()
    covered = 0
    total = 0
    for cls in root.iter("class"):
        filename = cls.get("filename", "")
        if not _is_gated(filename):
            continue
        for line in cls.iter("line"):
            total += 1
            if line.get("hits") != "0":
                covered += 1

    if total == 0:
        # Silently passing here would hide the engine going untested, which is
        # the one thing this check exists to prevent.
        print("FAILED: no engine modules found in the coverage report")
        return 1

    percent = 100.0 * covered / total
    status = "OK" if percent >= TARGET else "FAILED"
    print(
        f"{status}: engine logic coverage {percent:.1f}% "
        f"({covered}/{total} lines), target {TARGET}%"
    )
    return 0 if percent >= TARGET else 1


if __name__ == "__main__":
    raise SystemExit(main())
