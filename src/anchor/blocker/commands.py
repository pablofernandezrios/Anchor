"""Running system commands, in a way tests can stand in for.

The blocker drives ``nft``, ``resolvectl`` and ``systemctl``. Injecting the
runner means the logic that decides *what* to run can be tested without root
and without a firewall, leaving only the commands themselves to be proved on a
real machine.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

log = logging.getLogger("anchor-blockerd")


@dataclass(frozen=True, slots=True)
class Result:
    """What a command said."""

    code: int
    out: str = ""
    err: str = ""

    @property
    def ok(self) -> bool:
        return self.code == 0

    @property
    def text(self) -> str:
        return (self.out + self.err).strip()


class Runner(Protocol):
    """Runs a command and reports the result."""

    def __call__(self, args: Sequence[str], *, input_text: str | None = None) -> Result: ...


def run(args: Sequence[str], *, input_text: str | None = None) -> Result:
    """Run a command for real."""
    try:
        completed = subprocess.run(  # noqa: S603
            list(args),
            capture_output=True,
            text=True,
            input=input_text,
            timeout=30,
            check=False,
        )
    except FileNotFoundError:
        return Result(code=127, err=f"{args[0]}: not found")
    except subprocess.TimeoutExpired:
        return Result(code=124, err=f"{' '.join(args)}: timed out")
    return Result(code=completed.returncode, out=completed.stdout, err=completed.stderr)


class RecordingRunner:
    """A runner that remembers what it was asked to do, for tests."""

    def __init__(self, replies: dict[str, Result] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.replies = replies or {}

    def __call__(self, args: Sequence[str], *, input_text: str | None = None) -> Result:
        del input_text
        self.calls.append(list(args))
        for fragment, reply in self.replies.items():
            if fragment in " ".join(args):
                return reply
        return Result(code=0)

    def ran(self, fragment: str) -> bool:
        return any(fragment in " ".join(call) for call in self.calls)
