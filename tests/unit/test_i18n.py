"""Choosing a language, and surviving having no translations (SPEC 14)."""

from __future__ import annotations

import pytest

from anchor.gui.i18n import _, languages_for, setup


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
    def test_the_english_source_is_what_comes_out(self) -> None:
        # A checkout with nothing compiled must still run, in English.
        setup("es")
        assert _("Start session") == "Start session"

    def test_setting_up_returns_the_same_translator(self) -> None:
        translate = setup("")
        assert translate("Home") == _("Home")
