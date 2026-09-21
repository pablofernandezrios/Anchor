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

    custom: bool = False
    """Whether this is the user's own copy rather than the shipped list.

    Worth knowing on the Lists screen: a shipped category is replaced on every
    package update and a copy of it never is, and a user who edited one should
    be able to see which they are looking at.
    """

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "domains": sorted(self.domains),
            "apps": sorted(self.apps),
            "custom": self.custom,
        }


def parse_category(path: Path, *, custom: bool = False) -> Category | None:
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
        custom=custom,
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
    for directory, custom in ((shipped, False), (overrides, True)):
        if directory is None:
            continue
        try:
            files = sorted(directory.glob("*.toml"))
        except OSError:
            continue
        for path in files:
            category = parse_category(path, custom=custom)
            if category is not None:
                found[category.id] = category
    return found


def write_category(category: Category, directory: Path) -> Path:
    """Write a user's copy of a category, which replaces the shipped one.

    SPEC 12 wants lists that are both editable and updated with the package,
    which only works if the two live apart: the package owns
    ``/usr/share/anchor/categories`` and replaces it freely, and this writes
    into ``/etc/anchor/categories``, where nothing will overwrite it.

    Written whole rather than patched, and through a temporary file, so a
    machine that loses power in the middle of an edit has either the old list
    or the new one and never half of each (SPEC 6.1).
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{category.id}.toml"
    temporary = path.with_name(f".{path.name}.new")
    temporary.write_text(_as_toml(category), encoding="utf-8")
    temporary.replace(path)
    return path


def _as_toml(category: Category) -> str:
    lines = [
        "# Written by Anchor. This file replaces the category the package ships,",
        "# and package updates will not touch it.",
        f"name = {_quote(category.name)}",
        "",
        "domains = [",
        *(f"  {_quote(domain)}," for domain in sorted(category.domains)),
        "]",
        "",
        "apps = [",
        *(f"  {_quote(app)}," for app in sorted(category.apps)),
        "]",
    ]
    return "\n".join(lines) + "\n"


def _quote(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


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
