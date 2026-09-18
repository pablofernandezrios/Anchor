"""Shared plumbing for the Milestone 0 spikes.

These are throwaway prototypes whose job is to answer questions about the
system, not to become part of Anchor. They use the standard library only, both
because the root daemons must (SPEC 5.4) and because a spike that needs
installing is a worse experiment.

Every spike reports findings as structured records so the same script can print
a readable summary for a person and a JSON document for the report in
``docs/spikes/``.
"""

from __future__ import annotations

import json
import subprocess
import sys
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class Verdict(StrEnum):
    WORKS = "works"
    """The assumption in the specification holds."""

    FAILS = "fails"
    """The assumption does not hold. The specification needs revisiting."""

    UNAVAILABLE = "unavailable"
    """This machine cannot answer the question; try it elsewhere."""


@dataclass
class Finding:
    name: str
    verdict: Verdict
    detail: str
    evidence: str = ""

    def line(self) -> str:
        mark = {Verdict.WORKS: "PASS", Verdict.FAILS: "FAIL", Verdict.UNAVAILABLE: "SKIP"}
        return f"[{mark[self.verdict]}] {self.name}: {self.detail}"


@dataclass
class SpikeReport:
    spike: str
    question: str
    findings: list[Finding] = field(default_factory=list)

    def add(
        self, name: str, verdict: Verdict, detail: str, evidence: str = ""
    ) -> Finding:
        finding = Finding(name=name, verdict=verdict, detail=detail, evidence=evidence)
        self.findings.append(finding)
        print(finding.line(), flush=True)
        return finding

    @property
    def failed(self) -> bool:
        return any(finding.verdict is Verdict.FAILS for finding in self.findings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "spike": self.spike,
            "question": self.question,
            "verdict": "fails" if self.failed else "works",
            "findings": [asdict(finding) for finding in self.findings],
        }

    def write(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self.spike}.json"
        path.write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")
        return path


@dataclass
class Run:
    """The result of a command."""

    code: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.code == 0

    @property
    def text(self) -> str:
        return (self.out + self.err).strip()


def run(*args: str, input_text: str | None = None, timeout: float = 30.0) -> Run:
    """Run a command and capture everything it says."""
    try:
        completed = subprocess.run(
            args,
            capture_output=True,
            text=True,
            input=input_text,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return Run(code=127, out="", err=f"{args[0]}: not found")
    except subprocess.TimeoutExpired:
        return Run(code=124, out="", err=f"{' '.join(args)}: timed out")
    return Run(code=completed.returncode, out=completed.stdout, err=completed.stderr)


def have(command: str) -> bool:
    return run("sh", "-c", f"command -v {command}").ok


@contextmanager
def restored_file(path: Path) -> Iterator[Path]:
    """Put ``path`` back exactly as it was, whatever the spike does to it.

    Spikes touch DNS, firewall rules and unit files. Leaving any of that behind
    is not acceptable even on a throwaway machine, because the next spike would
    then be measuring the last one.
    """
    existed = path.exists()
    original = path.read_bytes() if existed else b""
    try:
        yield path
    finally:
        if existed:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(original)
        else:
            path.unlink(missing_ok=True)


def main(spike: Callable[[SpikeReport], None], report: SpikeReport) -> int:
    """Run ``spike``, write its findings, and exit the way CI expects."""
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("spike-results")
    print(f"=== {report.spike}: {report.question}\n", flush=True)
    try:
        spike(report)
    except Exception:
        report.add(
            "spike crashed",
            Verdict.FAILS,
            "the spike itself raised an exception",
            traceback.format_exc(),
        )

    path = report.write(out_dir)
    print(f"\nfindings written to {path}", flush=True)

    # A spike that cannot run is not a failure: it is an answer about where the
    # question can be asked. Only a contradicted assumption fails the build.
    return 1 if report.failed else 0
