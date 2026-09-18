#!/usr/bin/env python3
"""Turn spike result files into a readable summary.

Used by CI to write the job summary, and by hand to produce the tables that go
into ``docs/spikes/``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

MARK = {
    "works": "PASS",
    "fails": "FAIL",
    "unavailable": "SKIP",
    "ruled_out": "RULED OUT",
}


def load(directory: Path) -> list[dict[str, Any]]:
    return [
        json.loads(path.read_text(encoding="utf-8")) for path in sorted(directory.glob("*.json"))
    ]


def render(reports: list[dict[str, Any]]) -> str:
    if not reports:
        return "No spike results were produced.\n"

    lines = ["## Spike findings", ""]
    for report in reports:
        verdict = MARK.get(report["verdict"], "?")
        lines += [
            f"### {report['spike']} — {verdict}",
            "",
            f"_{report['question']}_",
            "",
            "| Check | Result | Detail |",
            "|---|---|---|",
        ]
        for finding in report["findings"]:
            detail = finding["detail"].replace("|", "\\|")
            lines.append(f"| {finding['name']} | {MARK.get(finding['verdict'], '?')} | {detail} |")
        lines.append("")
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: summarise.py <results-dir> [--check]", file=sys.stderr)
        return 2

    directory = Path(argv[0])
    check_only = "--check" in argv[1:]
    reports = load(directory)

    if check_only:
        failed = [report for report in reports if report["verdict"] == "fails"]
        for report in failed:
            print(f"{report['spike']} contradicts the specification:", file=sys.stderr)
            for finding in report["findings"]:
                if finding["verdict"] == "fails":
                    print(f"  - {finding['name']}: {finding['detail']}", file=sys.stderr)
        if not reports:
            print("no spike results were produced", file=sys.stderr)
            return 1
        return 1 if failed else 0

    print(render(reports), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
