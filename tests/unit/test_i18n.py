"""Choosing a language, and surviving having no translations (SPEC 14)."""

from __future__ import annotations

from pathlib import Path

import pytest

import anchor.gui.i18n as i18n
from anchor.gui.i18n import _, languages_for, setup


@pytest.fixture(autouse=True)
def _restore_the_translator() -> object:
    """Put the language back after every test here.

    ``setup`` installs a translator in a module global, which is what lets
    every screen call ``_`` without being handed one. It also means a test
    that switches to Spanish would leave every later test in Spanish, and the
    interface's own tests assert on the English.
    """
    before = i18n._translate
    yield
    i18n._translate = before


class TestChoosingALanguage:
    def test_a_stored_preference_wins_over_the_desktop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The case that matters: a Spanish desktop and a user who wants
        # Anchor in English. Following the desktop would overrule them.
        monkeypatch.setenv("LANGUAGE", "es_ES.UTF-8")
        assert languages_for("en") == ["en"]

    def test_nothing_stored_follows_the_desktop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LANGUAGE", "es_ES.UTF-8")
        assert languages_for("") == ["es"]

    def test_a_list_of_desktop_languages_is_kept_in_order(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LANGUAGE", "es:en_GB")
        assert languages_for("") == ["es", "en"]

    def test_a_language_anchor_does_not_speak_falls_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """gettext then finds no catalogue and answers in the source English."""
        monkeypatch.delenv("LANGUAGE", raising=False)
        monkeypatch.delenv("LC_ALL", raising=False)
        monkeypatch.delenv("LC_MESSAGES", raising=False)
        monkeypatch.setenv("LANG", "fr_FR.UTF-8")
        assert languages_for("") == ["fr"]

    def test_a_desktop_that_says_nothing_asks_for_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for name in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
            monkeypatch.delenv(name, raising=False)
        assert languages_for("") == []


class TestWithoutAnyTranslations:
    def test_the_english_source_is_what_comes_out(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A checkout with nothing compiled must still run, in English."""
        monkeypatch.setattr(i18n, "_SEARCH", (tmp_path,))
        setup("es")

        assert _("Start session") == "Start session"

    def test_setting_up_returns_the_same_translator(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(i18n, "_SEARCH", (tmp_path,))
        translate = setup("")

        assert translate("Home") == _("Home")


class TestWithTheSpanishCatalogue:
    """The catalogue this repository ships, compiled and read back."""

    def compiled(self, tmp_path: Path) -> Path:
        import importlib.util
        import sys

        root = Path(__file__).resolve().parents[2]
        spec = importlib.util.spec_from_file_location("anchor_po", root / "tools" / "po.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)

        module.write_mo(
            module.read_po(root / "po" / "es.po"),
            tmp_path / "es" / "LC_MESSAGES" / "anchor.mo",
        )
        return tmp_path

    def test_spanish_comes_out_when_spanish_is_asked_for(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(i18n, "_SEARCH", (self.compiled(tmp_path),))
        setup("es")

        assert _("Start session") == "Empezar sesión"

    def test_and_english_when_english_is(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """There is no English catalogue: the source is the English."""
        monkeypatch.setattr(i18n, "_SEARCH", (self.compiled(tmp_path),))
        setup("en")

        assert _("Start session") == "Start session"

    def test_a_sentence_with_a_placeholder_still_formats(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The failure the catalogue test guards against, from the other end."""
        monkeypatch.setattr(i18n, "_SEARCH", (self.compiled(tmp_path),))
        setup("es")

        assert _("Started at {time}").format(time="09:30") == "Empezó a las 09:30"
