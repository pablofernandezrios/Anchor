"""Categories: a name that stands for domains and applications (SPEC 9, 12).

A category is the useful unit for a person. "Social media" is one tick, and
behind it are two dozen domains and whichever applications belong with them:
SPEC 9's example is Discord, where blocking the program without
``discord.com`` would block nothing much at all.

The five shipped categories live in ``/usr/share/anchor/categories`` and are
replaced when the package is updated, so editing them would lose the edit. A
file of the same name in ``/etc/anchor/categories`` replaces the shipped one
entirely, which is how SPEC 12 asks for both: shipped lists that stay current,
and lists the user owns.

A file that cannot be read is skipped rather than fatal. A category that fails
to load blocks nothing, and refusing to start the engine over a stray character
in a list would be a far worse answer than blocking less for one session.
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("anchord")

#: Where the shipped categories live once installed.
DEFAULT_CATEGORY_DIR = Path("/usr/share/anchor/categories")


@dataclass(frozen=True, slots=True)
class Category:
    """One named bundle of domains and applications."""

    id: str
    """The file name without its suffix, which is what a profile stores."""

    name: str
    """What a person sees."""

    domains: frozenset[str] = frozenset()
    apps: frozenset[str] = frozenset()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "domains": sorted(self.domains),
            "apps": sorted(self.apps),
        }


def parse_category(path: Path) -> Category | None:
    """Read one category file, or ``None`` if it is not usable."""
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        log.warning("could not read the category %s: %s", path.name, error)
        return None
    except tomllib.TOMLDecodeError as error:
        log.warning("%s is not valid TOML and was skipped: %s", path.name, error)
        return None

    identifier = path.stem
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        log.warning("%s has no name and was skipped", path.name)
        return None

    return Category(
        id=identifier,
        name=name.strip(),
        domains=_strings(raw.get("domains"), path, "domains"),
        apps=_strings(raw.get("apps"), path, "apps"),
    )


def _strings(value: object, path: Path, field: str) -> frozenset[str]:
    if value is None:
        return frozenset()
    if not isinstance(value, list):
        log.warning("%s: %s is not a list and was ignored", path.name, field)
        return frozenset()

    wanted: set[str] = set()
    for item in value:
        if isinstance(item, str) and item.strip():
            wanted.add(item.strip().lower() if field == "domains" else item.strip())
        else:
            log.warning("%s: an entry in %s is not text and was ignored", path.name, field)
    return frozenset(wanted)


def load_categories(
    shipped: Path = DEFAULT_CATEGORY_DIR, overrides: Path | None = None
) -> dict[str, Category]:
    """Every category this machine knows, the user's copies winning."""
    found: dict[str, Category] = {}
    for directory in (shipped, overrides):
        if directory is None:
            continue
        try:
            files = sorted(directory.glob("*.toml"))
        except OSError:
            continue
        for path in files:
            category = parse_category(path)
            if category is not None:
                found[category.id] = category
    return found


@dataclass(frozen=True, slots=True)
class Resolved:
    """What a profile's categories add to it."""

    domains: frozenset[str] = frozenset()
    apps: frozenset[str] = frozenset()


def resolve(names: frozenset[str], known: dict[str, Category]) -> Resolved:
    """Turn the categories a profile names into domains and applications.

    A name nothing answers to is skipped with a note. A category the user
    deleted must not stop a session from starting, and blocking everything
    because a list went missing would be worse than blocking less.
    """
    domains: set[str] = set()
    apps: set[str] = set()
    for identifier in sorted(names):
        category = known.get(identifier)
        if category is None:
            log.info("the profile names a category that is not installed: %r", identifier)
            continue
        domains |= category.domains
        apps |= category.apps
    return Resolved(domains=frozenset(domains), apps=frozenset(apps))
