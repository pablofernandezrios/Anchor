"""The catalogue, and whether it still matches the source (SPEC 14).

Translations rot quietly: a sentence changes, the catalogue keeps the old one,
and the Spanish interface goes on saying something that is no longer true. So
the template is generated from the source and checked in, and this fails when
the two drift apart.

The last test is the one that matters most. A translation that loses a
placeholder does not look wrong in a text editor — it crashes at the moment
the sentence is formatted, in front of the one user who reads Spanish.
"""

from __future__ import annotations

import importlib.util
import re
import string
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
PO_DIR = ROOT / "po"
POT = PO_DIR / "anchor.pot"


def _tool() -> Any:
    """``tools/po.py``, which is a script rather than part of the package."""
    spec = importlib.util.spec_from_file_location("anchor_po_tool", ROOT / "tools" / "po.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


po = _tool()


def catalogues() -> list[Path]:
    return sorted(PO_DIR.glob("*.po"))


class TestTheTemplate:
    def test_it_is_up_to_date_with_the_source(self) -> None:
        """`python3 tools/po.py extract` is what to run when this fails."""
        entries = po.extract()
        rebuilt = po.write_pot(entries, POT)

        assert rebuilt == POT.read_text(encoding="utf-8")

    def test_it_holds_the_interface_s_own_words(self) -> None:
        entries = po.extract()

        assert "Start session" in entries
        assert "Emergency valve" in entries

    def test_the_words_looked_up_by_index_are_in_it_too(self) -> None:
        """Weekdays and months live in tuples, and N_() is what finds them."""
        entries = po.extract()

        for word in ("Mon", "Sun", "January", "December", "Home", "Settings"):
            assert word in entries, f"{word!r} would never be translated"


class TestSpanish:
    def test_there_is_a_spanish_catalogue(self) -> None:
        assert (PO_DIR / "es.po").exists()

    def test_every_string_is_translated(self) -> None:
        """`python3 tools/po.py check` prints what is missing."""
        entries = po.extract()
        gaps = po.missing(entries, po.read_po(PO_DIR / "es.po"))

        assert gaps == [], f"{len(gaps)} untranslated string(s)"

    def test_nothing_in_it_is_left_over_from_an_older_version(self) -> None:
        entries = po.extract()
        stale = [msgid for msgid in po.read_po(PO_DIR / "es.po") if msgid not in entries]

        assert stale == []

    def test_it_is_actually_in_spanish(self) -> None:
        catalogue = po.read_po(PO_DIR / "es.po")

        assert catalogue["Start session"] == "Empezar sesión"
        assert catalogue["Emergency valve"] == "Válvula de emergencia"


@pytest.mark.parametrize("catalogue", catalogues(), ids=lambda path: path.stem)
class TestPlaceholders:
    """A translation that drops a placeholder crashes when it is formatted."""

    def placeholders(self, text: str) -> set[str]:
        return {name for _literal, name, _spec, _conv in string.Formatter().parse(text) if name}

    def test_every_translation_keeps_its_placeholders(self, catalogue: Path) -> None:
        for msgid, msgstr in po.read_po(catalogue).items():
            if not msgstr:
                continue
            assert self.placeholders(msgid) == self.placeholders(msgstr), msgid

    def test_no_translation_invents_one(self, catalogue: Path) -> None:
        """Which would crash the same way, from the other direction."""
        for msgid, msgstr in po.read_po(catalogue).items():
            if not msgstr:
                continue
            assert self.placeholders(msgstr) <= self.placeholders(msgid), msgid

    def test_nothing_is_formatted_with_a_number_instead_of_a_name(self, catalogue: Path) -> None:
        """`{0}` would be one more thing a translator could silently break."""
        for msgid in po.read_po(catalogue):
            assert not re.search(r"\{\d", msgid), msgid


class TestCompiling:
    def test_a_catalogue_can_be_read_back_by_gettext(self, tmp_path: Path) -> None:
        """The whole point: what is written here is what gettext reads."""
        import gettext

        target = tmp_path / "es" / "LC_MESSAGES" / "anchor.mo"
        po.write_mo(po.read_po(PO_DIR / "es.po"), target)

        catalogue = gettext.translation("anchor", localedir=str(tmp_path), languages=["es"])
        assert catalogue.gettext("Start session") == "Empezar sesión"

    def test_an_untranslated_string_comes_back_as_the_english(self, tmp_path: Path) -> None:
        target = tmp_path / "es" / "LC_MESSAGES" / "anchor.mo"
        po.write_mo({"Start session": "Empezar sesión"}, target)

        import gettext

        catalogue = gettext.translation(
            "anchor", localedir=str(tmp_path), languages=["es"], fallback=True
        )
        assert catalogue.gettext("Nothing was translated") == "Nothing was translated"
