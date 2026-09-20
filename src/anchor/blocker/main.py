"""``anchor-blockerd``: DNS, nftables, policies and applications (SPEC 5.1).

The blocker executes the engine's orders. Blocking itself is still being built:
web blocking lands in Milestone 2 and application blocking in Milestone 4.

``--restore`` works now, and deliberately came first. It undoes everything
Anchor applies, whether or not a session is active, and the package removal
scripts call it. Building the way out before the way in is what keeps a
half-finished blocker from being able to strand a machine (SPEC 7.6, P4).
"""

from __future__ import annotations

import argparse
import logging
import sys

from anchor.blocker.constants import JOURNAL_NAME
from anchor.blocker.journal import Journal
from anchor.blocker.restore import restore_everything
from anchor.engine.paths import Paths

log = logging.getLogger("anchor-blockerd")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="anchor-blockerd",
        description="Anchor's blocker: DNS, firewall rules, browser policies and applications.",
    )
    parser.add_argument(
        "--restore",
        action="store_true",
        help="Undo everything Anchor applied and exit. Always allowed.",
    )
    parser.add_argument("--root", help="Run against a relocated tree, for development.")
    parser.add_argument("--verbose", action="store_true", help="Log at debug level.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    paths = Paths.resolve(args.root)

    if args.restore:
        journal = Journal(paths.state_dir / JOURNAL_NAME)
        report = restore_everything(journal)
        print(report.summary())
        for problem in report.problems:
            print(f"could not undo: {problem}", file=sys.stderr)
        if report.problems:
            print(
                "\nSome changes could not be undone. Anchor keeps nothing that can break "
                "networking outside /run, so a reboot clears the rest.",
                file=sys.stderr,
            )
            return 1
        return 0

    log.error(
        "anchor-blockerd does not block anything yet: web blocking is being built "
        "in Milestone 2 and application blocking in Milestone 4. "
        "Use --restore to undo anything Anchor has applied."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
