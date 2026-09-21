"""Words, in English or in Spanish (SPEC 14).

English is the default and Spanish is the translation, through gettext. Two
decisions shape this module.

**The interface decides its own words.** Every screen is built from a view
model that returns finished strings, so the translation happens there, next to
the sentence that needs it, and the GTK layer only draws. That keeps the
translated text inside the half of the interface that has tests.

**A missing catalogue is not an error.** ``gettext`` falls back to the msgid,
which is the English source string, so an Anchor running from a checkout with
nothing compiled is an Anchor in English rather than an Anchor that will not
start. Tests get the same fallback and can therefore assert on the English.
"""

from __future__ import annotations

import gettext as _gettext
import locale
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Final

log = logging.getLogger("anchor-gui")

#: The gettext domain, and the name of the compiled catalogue file.
DOMAIN: Final = "anchor"

#: Where compiled catalogues are looked for, in order. The first is where the
#: packages put them; the second is a checkout that has run ``tools/po.py``.
_SEARCH: Final = (
    Path("/usr/share/locale"),
    Path(__file__).resolve().parents[3] / "build" / "locale",
)

#: The languages SPEC 14 asks for. Anything else follows the desktop.
LANGUAGES: Final = ("en", "es")

_translate: Callable[[str], str] = lambda text: text  # noqa: E731


def locale_dir() -> Path | None:
    """The first directory that actually holds a catalogue, if any."""
    for candidate in _SEARCH:
        if (candidate / "es" / "LC_MESSAGES" / f"{DOMAIN}.mo").exists():
            return candidate
    return None


def languages_for(preference: str) -> list[str]:
    """Which catalogues to try, given a stored preference (SPEC 14).

    An empty preference follows the desktop, which is what a user who never
    opened Settings expects. A preference wins over the environment, which is
    what a user who did expects — including a user whose desktop is in Spanish
    and who wants Anchor in English.
    """
    chosen = preference.strip().lower()
    if chosen in LANGUAGES:
        return [chosen]

    from_environment = [
        value
        for name in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG")
        if (value := os.environ.get(name))
    ]
    wanted: list[str] = []
    for value in from_environment:
        for piece in value.split(":"):
            tag = piece.split(".")[0].split("_")[0].strip().lower()
            if tag and tag not in wanted:
                wanted.append(tag)
    return wanted


def setup(preference: str = "") -> Callable[[str], str]:
    """Choose the language and return the function that translates.

    Called once, before any view model is built. The returned function is also
    installed as this module's ``translate``, which is what :func:`_` uses, so
    that modules importing ``_`` do not each have to be handed a translator.
    """
    global _translate

    directory = locale_dir()
    languages = languages_for(preference)
    if directory is None:
        _translate = lambda text: text  # noqa: E731
        log.debug("no compiled translations found; using the English source")
    else:
        catalogue = _gettext.translation(
            DOMAIN, localedir=str(directory), languages=languages or None, fallback=True
        )
        _translate = catalogue.gettext

    # Only for the parts of GTK that format numbers and dates themselves.
    # A locale this machine does not have must not stop Anchor from starting.
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        log.debug("could not set the locale; falling back to C")

    return _translate


def _(text: str) -> str:
    """Translate one string.

    Named as gettext names it, because every tool that reads source for
    translatable strings looks for this name — including Anchor's own
    extractor in ``tools/po.py``.
    """
    return _translate(text)


def N_(text: str) -> str:  # noqa: N802 - gettext's own spelling
    """Mark a string for translation without translating it yet.

    For the handful of words that live in a tuple and are looked up by
    index — weekdays, months. ``_(DAY_NAMES[index])`` translates at the
    moment of use, but it hands the extractor a variable rather than a
    string, so the words would never reach the catalogue at all. This marks
    them where they are written and changes nothing at runtime.
    """
    return text


def ngettext(singular: str, plural: str, count: int) -> str:
    """The one place a count changes the sentence.

    Spanish and English agree on where the boundary is, so the plain
    ``gettext`` rules are enough and no plural forms beyond ``n != 1`` are
    needed in the catalogue.
    """
    return _(singular if count == 1 else plural)
