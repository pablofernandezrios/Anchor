"""Undoing everything Anchor did to the machine (SPEC 7.6).

Called when a session ends, when Anchor is removed, and by ``anchor doctor``
when it finds something it can put right. Uninstalling is always allowed, so
this runs whether or not a session is active and never refuses.

Three rules shape it:

* **Nothing stops at the first failure.** A browser policy that cannot be
  written back must not leave the firewall rules in place.
* **The table goes even without a journal.** The name is a constant, so the
  rules can be removed by a removal script that has lost its record of them.
* **The absence of something is not a failure.** Restoring a machine that was
  already clean is the normal case, not an error.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from anchor.blocker.constants import (
    NFT_FAMILY,
    NFT_TABLE,
    REFUSE_STOP_FILENAME,
    REFUSE_STOP_UNITS,
    RESOLVED_DROP_IN,
    SYSTEMD_RUNTIME_DIR,
)
from anchor.blocker.journal import Journal
from anchor.system.commands import Result, Runner, run

log = logging.getLogger("anchor-blockerd")

#: What nftables says when the table is not there. Not a failure: it means
#: somebody already removed it, or a session never started.
_ABSENT = ("no such file or directory", "does not exist", "no such table")


@dataclass(slots=True)
class RestoreReport:
    """What was undone, and what could not be."""

    removed_table: bool = False
    reverted_links: list[str] = field(default_factory=list)
    restored_files: int = 0
    removed_drop_ins: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.problems

    def summary(self) -> str:
        parts: list[str] = []
        if self.removed_table:
            parts.append(f"removed the {NFT_FAMILY} {NFT_TABLE} table")
        if self.reverted_links:
            parts.append(f"reverted DNS on {', '.join(self.reverted_links)}")
        if self.restored_files:
            parts.append(f"restored {self.restored_files} file(s)")
        if self.removed_drop_ins:
            parts.append(f"removed {len(self.removed_drop_ins)} systemd drop-in(s)")
        if not parts:
            return "nothing to undo: the machine was already as Anchor found it"
        return "; ".join(parts)


def _is_absent(result: Result) -> bool:
    return any(phrase in result.text.lower() for phrase in _ABSENT)


def restore_everything(
    journal: Journal,
    *,
    runner: Runner = run,
    resolved_drop_in: Path = RESOLVED_DROP_IN,
    systemd_runtime_dir: Path = SYSTEMD_RUNTIME_DIR,
) -> RestoreReport:
    """Put the machine back. Safe to call at any time, including twice."""
    report = RestoreReport()
    applied = journal.load()

    _remove_table(report, journal, runner)
    _revert_links(report, journal, applied.links, runner=runner)
    _remove_resolved_drop_in(report, resolved_drop_in, runner=runner)
    _remove_refusal_drop_ins(report, systemd_runtime_dir, runner=runner)

    before = len(journal.load().files)
    problems = journal.restore()
    report.restored_files = before - len(problems)
    report.problems.extend(problems)

    log.info("restore: %s", report.summary())
    return report


def _remove_table(report: RestoreReport, journal: Journal, runner: Runner) -> None:
    """Delete Anchor's nftables table.

    Attempted unconditionally. The journal may be missing or damaged, and
    leaving a redirect in place would be the one failure a user could not
    diagnose: every name would resolve through a resolver that is not running.
    """
    result = runner(["nft", "delete", "table", NFT_FAMILY, NFT_TABLE])
    if result.ok:
        report.removed_table = True
    elif not _is_absent(result):
        # Only a genuine failure keeps the record: the table is still there, so
        # a later restore has to try again. An absent table is already undone.
        report.problems.append(f"could not remove the nftables table: {result.text}")
        return

    journal.clear_nft_table()


def _revert_links(
    report: RestoreReport, journal: Journal, links: list[str], *, runner: Runner
) -> None:
    """Hand each link's DNS back to whatever configured it before."""
    for link in links:
        result = runner(["resolvectl", "revert", link])
        if result.ok:
            report.reverted_links.append(link)
        else:
            report.problems.append(f"could not revert DNS on {link}: {result.text}")
    if links:
        journal.clear_links()


def _remove_resolved_drop_in(report: RestoreReport, path: Path, *, runner: Runner) -> None:
    if not path.exists():
        return
    try:
        path.unlink()
    except OSError as error:
        report.problems.append(f"could not remove {path}: {error}")
        return

    report.removed_drop_ins.append(str(path))
    result = runner(["systemctl", "restart", "systemd-resolved"])
    if not result.ok:
        report.problems.append(f"could not restart systemd-resolved: {result.text}")


def _remove_refusal_drop_ins(report: RestoreReport, runtime_dir: Path, *, runner: Runner) -> None:
    """Clear the drop-ins that refuse a manual stop (SPEC 5.3).

    Removal has to be able to stop the services it is removing, and a session
    that was active when the package was removed would otherwise leave them
    unstoppable.
    """
    removed = False
    for unit in REFUSE_STOP_UNITS:
        drop_in = runtime_dir / f"{unit}.d" / REFUSE_STOP_FILENAME
        if not drop_in.exists():
            continue
        try:
            drop_in.unlink()
            report.removed_drop_ins.append(str(drop_in))
            removed = True
            parent = drop_in.parent
            if not any(parent.iterdir()):
                parent.rmdir()
        except OSError as error:
            report.problems.append(f"could not remove {drop_in}: {error}")

    if removed:
        result = runner(["systemctl", "daemon-reload"])
        if not result.ok:
            report.problems.append(f"could not reload systemd: {result.text}")
