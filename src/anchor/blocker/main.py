"""``anchor-blockerd``: DNS, nftables, policies and applications (SPEC 5.1).

The blocker executes the engine's orders. Blocking itself is still being built:
web blocking lands in Milestone 2 and application blocking in Milestone 4.

``--restore`` undoes everything Anchor applies, whether or not a session is
active, and the package removal scripts call it. It was built before anything
that applies a block, which is what keeps a half-finished blocker from being
able to strand a machine (SPEC 7.6, P4).

Application blocking arrives in Milestone 4.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
from pathlib import Path
from types import FrameType

from anchor.blocker.constants import JOURNAL_NAME, RESOLVER_PORT
from anchor.blocker.daemon import BlockerDaemon, Lists
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
    parser.add_argument(
        "--port",
        type=int,
        default=RESOLVER_PORT,
        help=f"Port for Anchor's resolver (default {RESOLVER_PORT}).",
    )
    parser.add_argument(
        "--data-dir",
        help="Where the shipped lists live. Defaults to the installed location.",
    )
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

    if os.geteuid() != 0:
        log.error(
            "anchor-blockerd needs root: it loads firewall rules and writes "
            "managed browser policies"
        )
        return 1

    lists = Lists.load(Path(args.data_dir)) if args.data_dir else Lists.load()
    daemon = BlockerDaemon(paths, lists=lists, resolver_port=args.port)

    def stop(signum: int, _frame: FrameType | None) -> None:
        log.info("received signal %d, stopping", signum)
        daemon.stop()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    try:
        daemon.run()
    finally:
        # Whatever happened, do not leave the machine behind a resolver that is
        # no longer running (P4).
        daemon.stop()
        if daemon.applied:
            log.info("undoing the blocks this daemon applied before exiting")
            daemon.undo()
    return 0


if __name__ == "__main__":
    sys.exit(main())
