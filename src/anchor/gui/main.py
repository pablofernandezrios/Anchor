"""``anchor-gui``: the interface (SPEC 5.1, 14).

A thin client like every other. It holds no state the engine does not, it
decides nothing, and closing it stops nothing: a session runs in the engine,
and the window is only a way to look at it.
"""

from __future__ import annotations

import argparse
import logging
import sys

from anchor import __version__
from anchor.engine.paths import Paths
from anchor.gui.i18n import setup

log = logging.getLogger("anchor-gui")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="anchor-gui", description=__doc__)
    parser.add_argument("--root", help="Talk to an engine on a relocated tree. Development only.")
    parser.add_argument("--verbose", action="store_true", help="Log at debug level.")
    parser.add_argument("--version", action="version", version=f"anchor {__version__}")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    paths = Paths.resolve(args.root)

    # The language before the first window, so no screen is built in one
    # language and redrawn in another.
    setup(_language(paths))

    try:
        from anchor.gui.app import AnchorApplication
    except (ImportError, ValueError) as error:
        print(
            f"anchor-gui: the interface needs PyGObject with GTK 4 and libadwaita ({error}).\n"
            "Install python3-gi, gir1.2-gtk-4.0 and gir1.2-adw-1. "
            "Everything Anchor does is also in `anchor --help`.",
            file=sys.stderr,
        )
        return 1

    return int(AnchorApplication(paths.engine_socket).run([]))


def _language(paths: object) -> str:
    """The stored language preference, if the engine can be asked for it.

    Asked directly rather than through the window, because the window's words
    are built as it is constructed. An engine that is not running yet leaves
    the desktop's own language in charge, which is the right answer anyway.
    """
    from anchor.cli.client import EngineClient, EngineUnreachableError

    try:
        with EngineClient(paths.engine_socket, timeout=2.0) as client:  # type: ignore[attr-defined]
            response = client.call("config.get", {"key": "language"})
    except (EngineUnreachableError, OSError):
        return ""

    if not response.ok:
        return ""
    settings = response.result.get("settings") or []
    return str(settings[0].get("value", "")) if settings else ""


if __name__ == "__main__":
    sys.exit(main())
