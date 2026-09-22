"""Choosing a language, and surviving having no translations (SPEC 14)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

import anchor.gui.i18n as i18n
from anchor.gui.i18n import _, languages_for

# Never imported by name: a module-level `setup` is pytest 7's xunit hook, so
# pytest calls it with the module as its argument and every test in the file
# errors before it runs. pytest 8 dropped the hook, which is why this passed
# here and broke inside the package build, on the pytest Ubuntu ships.


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


#: Everything gettext reads a language out of. A test that sets one of these
#: and leaves the rest alone is testing the machine it happens to run on:
#: continuous integration sets LANG=C.UTF-8, this machine sets nothing, and
#: the same assertion was true here and false there.
ENVIRONMENT = ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG")


@pytest.fixture
def desktop(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Say what the desktop asks for, and nothing else."""

    def say(**values: str) -> None:
        for name in ENVIRONMENT:
            monkeypatch.delenv(name, raising=False)
        for name, value in values.items():
            monkeypatch.setenv(name, value)

    return say


class TestChoosingALanguage:
    def test_a_stored_preference_wins_over_the_desktop(self, desktop: Callable[..., None]) -> None:
        # The case that matters: a Spanish desktop and a user who wants
        # Anchor in English. Following the desktop would overrule them.
        desktop(LANGUAGE="es_ES.UTF-8")
        assert languages_for("en") == ["en"]

    def test_nothing_stored_follows_the_desktop(self, desktop: Callable[..., None]) -> None:
        desktop(LANGUAGE="es_ES.UTF-8")
        assert languages_for("") == ["es"]

    def test_a_list_of_desktop_languages_is_kept_in_order(
        self, desktop: Callable[..., None]
    ) -> None:
        desktop(LANGUAGE="es:en_GB")
        assert languages_for("") == ["es", "en"]

    def test_a_language_anchor_does_not_speak_falls_through(
        self, desktop: Callable[..., None]
    ) -> None:
        """gettext then finds no catalogue and answers in the source English."""
        desktop(LANG="fr_FR.UTF-8")
        assert languages_for("") == ["fr"]

    def test_a_desktop_that_says_nothing_asks_for_nothing(
        self, desktop: Callable[..., None]
    ) -> None:
        desktop()
        assert languages_for("") == []

    def test_the_c_locale_is_not_a_language(self, desktop: Callable[..., None]) -> None:
        """`C` and `POSIX` mean "no locale", which is not something to ask for.

        The machine that found this runs continuous integration with
        LANG=C.UTF-8, where a Spanish desktop asked gettext for Spanish and
        then for a language called "c".
        """
        desktop(LANGUAGE="es_ES.UTF-8", LANG="C.UTF-8")
        assert languages_for("") == ["es"]

    def test_and_a_machine_with_only_that_asks_for_nothing(
        self, desktop: Callable[..., None]
    ) -> None:
        desktop(LANG="C.UTF-8", LC_ALL="POSIX")
        assert languages_for("") == []


class TestWithoutAnyTranslations:
    def test_the_english_source_is_what_comes_out(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A checkout with nothing compiled must still run, in English."""
        monkeypatch.setattr(i18n, "_SEARCH", (tmp_path,))
        i18n.setup("es")

        assert _("Start session") == "Start session"

    def test_setting_up_returns_the_same_translator(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(i18n, "_SEARCH", (tmp_path,))
        translate = i18n.setup("")

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
        i18n.setup("es")

        assert _("Start session") == "Empezar sesión"

    def test_and_english_when_english_is(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """There is no English catalogue: the source is the English."""
        monkeypatch.setattr(i18n, "_SEARCH", (self.compiled(tmp_path),))
        i18n.setup("en")

        assert _("Start session") == "Start session"

    def test_a_sentence_with_a_placeholder_still_formats(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The failure the catalogue test guards against, from the other end."""
        monkeypatch.setattr(i18n, "_SEARCH", (self.compiled(tmp_path),))
        i18n.setup("es")

        assert _("Started at {time}").format(time="09:30") == "Empezó a las 09:30"
