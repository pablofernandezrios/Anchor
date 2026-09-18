#!/usr/bin/env python3
"""Enforce the coverage target for engine logic (SPEC 18).

The 80% target applies to the modules the specification names: sessions,
ratchet, schedules, breaks and time. The rest of the tree is not gated, so a
thin client without tests cannot mask an untested engine.
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

TARGET = 80.0

# Modules the specification puts under the coverage target.
GATED_PREFIXES = (
    "anchor/engine/",
    "anchor/protocol/",
)


def main() -> int:
    report = Path("coverage.xml")
    if not report.exists():
        import subprocess

        subprocess.run([sys.executable, "-m", "coverage", "xml"], check=True)

    root = ET.parse(report).getroot()
    covered = 0
    total = 0
    for cls in root.iter("class"):
        filename = cls.get("filename", "")
        if not filename.startswith(GATED_PREFIXES):
            continue
        for line in cls.iter("line"):
            total += 1
            if line.get("hits") != "0":
                covered += 1

    if total == 0:
        print("no gated modules found; nothing to check")
        return 0

    percent = 100.0 * covered / total
    status = "OK" if percent >= TARGET else "FAILED"
    print(
        f"{status}: engine logic coverage {percent:.1f}% "
        f"({covered}/{total} lines), target {TARGET}%"
    )
    return 0 if percent >= TARGET else 1


if __name__ == "__main__":
    raise SystemExit(main())
