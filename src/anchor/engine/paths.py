"""Where Anchor keeps its files (SPEC 6.1).

The defaults are the installed locations. Everything is routed through
:class:`Paths` so that tests, and the ``ANCHOR_ROOT`` development override, can
place the whole tree somewhere harmless instead of writing to ``/var``.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Self

DEFAULT_ETC: Final = Path("/etc/anchor")
DEFAULT_STATE_DIR: Final = Path("/var/lib/anchor")
DEFAULT_RUNTIME_DIR: Final = Path("/run/anchor")

#: Set this to a directory to relocate the whole tree during development.
ROOT_ENV_VAR: Final = "ANCHOR_ROOT"

#: Statistics retention, in days (SPEC 13, confirmed by the owner).
DEFAULT_RETENTION_DAYS: Final = 90


@dataclass(frozen=True, slots=True)
class Paths:
    """Every path the engine touches."""

    etc_dir: Path = DEFAULT_ETC
    state_dir: Path = DEFAULT_STATE_DIR
    runtime_dir: Path = DEFAULT_RUNTIME_DIR

    @classmethod
    def resolve(cls, root: str | os.PathLike[str] | None = None) -> Self:
        """Return the installed paths, or paths under ``root`` if given.

        ``root`` falls back to ``ANCHOR_ROOT``, which lets a developer run the
        engine without touching the real system directories.
        """
        if root is None:
            root = os.environ.get(ROOT_ENV_VAR)
        if root is None:
            return cls()
        base = Path(root)
        return cls(
            etc_dir=base / "etc" / "anchor",
            state_dir=base / "var" / "lib" / "anchor",
            runtime_dir=base / "run" / "anchor",
        )

    @property
    def settings_file(self) -> Path:
        """``anchor.toml``: install-level settings, edited by hand."""
        return self.etc_dir / "anchor.toml"

    @property
    def config_file(self) -> Path:
        """Profiles, lists, schedules and preferences."""
        return self.state_dir / "config.json"

    @property
    def state_file(self) -> Path:
        """Active session, skips used this week, pending valve requests."""
        return self.state_dir / "state.json"

    @property
    def stats_db(self) -> Path:
        return self.state_dir / "stats.db"

    @property
    def key_file(self) -> Path:
        """The HMAC key. Mode 0600, root only."""
        return self.state_dir / ".key"

    @property
    def engine_socket(self) -> Path:
        return self.runtime_dir / "engine.sock"

    def ensure_directories(self) -> None:
        """Create the directories Anchor owns, with sensible modes."""
        self.etc_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o750)
        self.runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o755)


@dataclass(frozen=True, slots=True)
class Settings:
    """Install-level settings read from ``anchor.toml`` (SPEC 6.1)."""

    owner_uid: int
    retention_days: int = DEFAULT_RETENTION_DAYS

    @classmethod
    def load(cls, path: Path) -> Self:
        """Read ``anchor.toml``.

        The owner UID decides who may give the engine orders (SPEC 5.2), so a
        missing or unreadable file is an error rather than a silent default:
        guessing here would either lock the owner out or let anyone in.
        """
        try:
            raw: dict[str, Any] = tomllib.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise FileNotFoundError(
                f"{path} is missing; it is written when Anchor is installed"
            ) from exc
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f"{path} is not valid TOML: {exc}") from exc

        install = raw.get("install", {})
        if not isinstance(install, dict) or "owner_uid" not in install:
            raise ValueError(f"{path} must set install.owner_uid")

        owner_uid = install["owner_uid"]
        if not isinstance(owner_uid, int) or isinstance(owner_uid, bool) or owner_uid < 0:
            raise ValueError(f"{path}: install.owner_uid must be a non-negative integer")

        stats = raw.get("statistics", {})
        retention = DEFAULT_RETENTION_DAYS
        if isinstance(stats, dict) and "retention_days" in stats:
            candidate = stats["retention_days"]
            if not isinstance(candidate, int) or isinstance(candidate, bool) or candidate < 1:
                raise ValueError(f"{path}: statistics.retention_days must be a positive integer")
            retention = candidate

        return cls(owner_uid=owner_uid, retention_days=retention)

    def to_toml(self) -> str:
        return (
            "# Anchor install-level settings (SPEC 6.1).\n"
            "# Written when the package is installed. Safe to edit by hand.\n"
            "\n"
            "[install]\n"
            "# The user allowed to give the engine orders, besides root.\n"
            f"owner_uid = {self.owner_uid}\n"
            "\n"
            "[statistics]\n"
            "# How long statistics are kept, in days.\n"
            f"retention_days = {self.retention_days}\n"
        )
