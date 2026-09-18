"""``anchord``: the engine service (SPEC 5.1)."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from types import FrameType

from anchor.engine.core import Engine
from anchor.engine.paths import Paths, Settings
from anchor.engine.service import EngineServer

log = logging.getLogger("anchord")

#: How often the engine re-checks the clock and publishes a tick.
TICK_SECONDS = 1.0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="anchord", description="The Anchor engine.")
    parser.add_argument("--root", help="Run against a relocated tree, for development.")
    parser.add_argument(
        "--owner-uid",
        type=int,
        help="Override the owner from anchor.toml. Development only.",
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
    paths.ensure_directories()

    if args.owner_uid is not None:
        settings = Settings(owner_uid=args.owner_uid)
    else:
        try:
            settings = Settings.load(paths.settings_file)
        except (FileNotFoundError, ValueError) as error:
            log.error("%s", error)
            return 1

    engine = Engine(paths, settings)
    engine.load()

    server = EngineServer(engine)
    log.info("listening on %s for uid 0 and uid %d", paths.engine_socket, settings.owner_uid)

    stopping = threading.Event()

    def stop(signum: int, _frame: FrameType | None) -> None:
        log.info("received signal %d, shutting down", signum)
        stopping.set()
        # shutdown() waits for serve_forever() to return, and this handler runs
        # on the very thread that is inside serve_forever(), so calling it here
        # would deadlock: the service would hang on every stop until systemd
        # gave up waiting and sent SIGKILL.
        threading.Thread(target=server.shutdown, name="anchor-stop", daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    def ticker() -> None:
        while not stopping.wait(TICK_SECONDS):
            try:
                server.tick()
            except Exception:
                log.exception("the tick loop hit an error")

    threading.Thread(target=ticker, name="anchor-tick", daemon=True).start()

    try:
        server.serve_forever()
    finally:
        server.server_close()
        paths.engine_socket.unlink(missing_ok=True)
        engine.save()
    return 0


if __name__ == "__main__":
    sys.exit(main())
