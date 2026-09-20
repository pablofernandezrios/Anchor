"""A record of everything Anchor changed, so it can all be put back (SPEC 7.6).

Uninstalling is always allowed. That one sentence decides the shape of this
module: the restore path must work when no session is running, when one is,
when the engine is dead, and when the HMAC key is gone. So the journal is plain
JSON with no signature. An integrity check here could only ever refuse to give
someone their machine back.

The journal is also how Anchor fails open (P4). Anything that would break
networking if Anchor were not running is written to ``/run``, which a reboot
clears, so the worst case of a machine left with a half-applied Anchor is one
restart away from working. Only files that cannot break networking, such as
browser policies, are written under ``/etc``, and those are restored from here.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Self

log = logging.getLogger("anchor-blockerd")

_VERSION = 1


@dataclass(frozen=True, slots=True)
class FileRecord:
    """One file Anchor wrote, and what was there before."""

    path: str
    existed: bool
    original: str | None
    created_dirs: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Self:
        return cls(
            path=str(raw["path"]),
            existed=bool(raw["existed"]),
            original=raw.get("original"),
            created_dirs=list(raw.get("created_dirs", [])),
        )


@dataclass(slots=True)
class Applied:
    """Everything currently applied to the system."""

    version: int = _VERSION
    files: list[FileRecord] = field(default_factory=list)
    nft_table: bool = False
    links: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "files": [asdict(record) for record in self.files],
            "nft_table": self.nft_table,
            "links": list(self.links),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Self:
        return cls(
            version=int(raw.get("version", _VERSION)),
            files=[FileRecord.from_dict(item) for item in raw.get("files", [])],
            nft_table=bool(raw.get("nft_table", False)),
            links=list(raw.get("links", [])),
        )

    @property
    def is_empty(self) -> bool:
        return not self.files and not self.nft_table and not self.links


class Journal:
    """Writes files through a record of how to undo them."""

    __slots__ = ("path",)

    def __init__(self, path: Path) -> None:
        self.path = path

    # -- reading and writing the journal itself -------------------------

    def load(self) -> Applied:
        """Return what is currently applied.

        A journal that cannot be read comes back empty rather than raising.
        Refusing to restore because the record of what to restore is damaged
        would be the worst possible moment to be strict.
        """
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return Applied()
        except (OSError, json.JSONDecodeError) as error:
            log.warning(
                "the journal at %s is unreadable (%s); assuming nothing was applied, "
                "and falling back to removing Anchor's rules unconditionally",
                self.path,
                error,
            )
            return Applied()

        if not isinstance(raw, dict):
            return Applied()
        return Applied.from_dict(raw)

    def save(self, applied: Applied) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.tmp")
        tmp.write_text(json.dumps(applied.to_dict(), indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.path)

    # -- recording changes ----------------------------------------------

    def write_file(self, target: Path, content: str, *, mode: int = 0o644) -> None:
        """Write ``target``, remembering what was there first."""
        created: list[str] = []
        missing = target.parent
        while not missing.exists():
            created.append(str(missing))
            missing = missing.parent

        existed = target.exists()
        original = target.read_text(encoding="utf-8") if existed else None

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        target.chmod(mode)

        applied = self.load()
        # Keep the first record for a path: it holds the genuine original.
        if not any(record.path == str(target) for record in applied.files):
            applied.files.append(
                FileRecord(
                    path=str(target),
                    existed=existed,
                    original=original,
                    # Innermost first, so removal happens in that order.
                    created_dirs=created,
                )
            )
            self.save(applied)

    def record_nft_table(self) -> None:
        applied = self.load()
        applied.nft_table = True
        self.save(applied)

    def clear_nft_table(self) -> None:
        applied = self.load()
        applied.nft_table = False
        self.save(applied)

    def record_link(self, name: str) -> None:
        applied = self.load()
        if name not in applied.links:
            applied.links.append(name)
            self.save(applied)

    def clear_links(self) -> None:
        applied = self.load()
        applied.links = []
        self.save(applied)

    # -- putting it back -------------------------------------------------

    def restore(self) -> list[str]:
        """Put every recorded file back. Returns what could not be undone.

        Files are all this can undo by itself. The nftables table and the
        reconfigured links are recorded here but torn down by
        :mod:`anchor.blocker.restore`, which can run the commands; it reads
        these flags and clears them.

        One file failing must not strand the rest, so every record is attempted
        and the failures are collected rather than raised.
        """
        applied = self.load()
        problems: list[str] = []

        for record in reversed(applied.files):
            target = Path(record.path)
            try:
                if record.existed and record.original is not None:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(record.original, encoding="utf-8")
                else:
                    target.unlink(missing_ok=True)
                    self._remove_created_dirs(record)
            except OSError as error:
                problems.append(f"{target}: {error}")
                log.warning("could not restore %s: %s", target, error)

        applied.files = []
        self.save(applied)
        return problems

    @staticmethod
    def _remove_created_dirs(record: FileRecord) -> None:
        """Remove directories Anchor made, and only if they are empty.

        A managed-policy directory is shared with whatever else manages
        policies on the machine, so emptiness is the test: another program's
        file in there means the directory was not ours to remove.
        """
        for name in record.created_dirs:
            directory = Path(name)
            try:
                if directory.is_dir() and not any(directory.iterdir()):
                    directory.rmdir()
            except OSError:
                # Someone else's file arrived, or the directory is gone. Either
                # way it is not ours to remove.
                return
