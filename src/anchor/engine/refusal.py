"""Refusing to be stopped by hand while a session runs (SPEC 5.3).

While a session is active the engine writes ``RefuseManualStop=yes`` into a
drop-in for both root units and reloads systemd, so ``systemctl stop anchord``
is refused. When the session ends the drop-ins are removed and the services
behave normally again.

The drop-ins go under ``/run/systemd/system`` rather than ``/etc``, which
decides three things at once:

* **Uninstalling still works.** SPEC 7.6 says removal is always allowed, and a
  package cannot remove a service it is not permitted to stop. Removal clears
  these first.
* **A reboot clears them.** ``/run`` is a tmpfs, so a machine that somehow ends
  up with stale drop-ins is one restart from normal. The engine writes them
  again on boot if a session really is still running.
* **Nothing is left behind by a crash.** The refusal only ever outlives Anchor
  until the next boot.

Milestone 0 spike 7 confirmed the whole cycle on systemd 255: the stop is
refused, the service survives, and removing the drop-in hands control straight
back.

This is friction, not a lock. Root can delete the file, and SPEC 16 says so.
"""

from __future__ import annotations

import logging
from pathlib import Path

from anchor.system.commands import Runner, run

log = logging.getLogger("anchord")

#: The units that must not be stopped by hand mid-session.
UNITS = ("anchord.service", "anchor-blockerd.service")

#: Where systemd reads runtime drop-ins.
RUNTIME_DIR = Path("/run/systemd/system")

#: The file Anchor owns inside each unit's drop-in directory.
DROP_IN_NAME = "anchor-session.conf"

BODY = (
    "# Written by Anchor while a session is active (SPEC 5.3).\n"
    "# Removed when the session ends. Lives under /run, so a reboot clears it.\n"
    "[Unit]\n"
    "RefuseManualStop=yes\n"
)


def _drop_in(runtime_dir: Path, unit: str) -> Path:
    return runtime_dir / f"{unit}.d" / DROP_IN_NAME


def is_applied(runtime_dir: Path = RUNTIME_DIR) -> bool:
    """Whether the refusal is currently in place for every unit."""
    return all(_drop_in(runtime_dir, unit).exists() for unit in UNITS)


def apply_refusal(*, runtime_dir: Path = RUNTIME_DIR, runner: Runner = run) -> bool:
    """Refuse manual stops. Returns whether anything changed.

    Failing here does not fail the session. The refusal is friction, and a
    session that blocks without it is still a session; one that refuses to
    start because systemd would not reload would be worse than the problem.
    """
    if is_applied(runtime_dir):
        return False

    written = False
    for unit in UNITS:
        target = _drop_in(runtime_dir, unit)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(BODY, encoding="utf-8")
            written = True
        except OSError as error:
            log.warning("could not write %s: %s", target, error)

    if not written:
        return False

    result = runner(["systemctl", "daemon-reload"])
    if not result.ok:
        log.warning(
            "systemd would not reload (%s); the stop refusal may not be in effect",
            result.text[:200],
        )
        return False

    log.info("manual stops refused for %s while the session runs", ", ".join(UNITS))
    return True


def remove_refusal(*, runtime_dir: Path = RUNTIME_DIR, runner: Runner = run) -> bool:
    """Allow manual stops again. Returns whether anything changed.

    Safe to call when nothing is applied, which is the common case: it runs
    every time a session ends, and at startup when none was running.
    """
    removed = False
    for unit in UNITS:
        target = _drop_in(runtime_dir, unit)
        if not target.exists():
            continue
        try:
            target.unlink()
            removed = True
            parent = target.parent
            if not any(parent.iterdir()):
                parent.rmdir()
        except OSError as error:
            log.warning("could not remove %s: %s", target, error)

    if not removed:
        return False

    result = runner(["systemctl", "daemon-reload"])
    if not result.ok:
        log.warning("systemd would not reload (%s)", result.text[:200])

    log.info("manual stops allowed again")
    return True
