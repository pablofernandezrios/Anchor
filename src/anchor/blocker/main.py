"""``anchor-blockerd``: DNS, nftables, policies and applications (SPEC 5.1).

The blocker executes the engine's orders. Nothing is implemented yet: web
blocking arrives in Milestone 2 and application blocking in Milestone 4, both
behind the spikes in ``docs/spikes/``. The entry point exists so the packaging
and the unit files have something to point at, and so that starting it by hand
says plainly that it does not block anything yet rather than appearing to work.
"""

from __future__ import annotations

import logging
import sys

log = logging.getLogger("anchor-blockerd")


def main(argv: list[str] | None = None) -> int:
    del argv
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    log.error(
        "anchor-blockerd does nothing yet: web blocking lands in Milestone 2 and "
        "application blocking in Milestone 4. Nothing is being blocked."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
