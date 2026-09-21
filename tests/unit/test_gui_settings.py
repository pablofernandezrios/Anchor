"""The Settings screen (SPEC 7.2, 13, 14)."""

from __future__ import annotations

from typing import Any

from anchor.gui.settings import set_request, settings_view

DESCRIBED: list[dict[str, Any]] = [
    {
        "key": "firm_wait_seconds",
        "value": 900,
        "is_default": True,
        "explain": "How long a Firm session makes you wait before it lets go.",
        "during_session": False,
    },
    {
        "key": "phrase_length",
        "value": 150,
        "is_default": True,
        "explain": "How many characters the random phrase has.",
        "during_session": False,
    },
    {
        "key": "retention_days",
        "value": 90,
        "is_default": True,
        "explain": "How long statistics are kept before they are forgotten.",
        "during_session": True,
    },
    {
        "key": "language",
        "value": "",
        "is_default": True,
        "explain": "The interface language. Empty follows the desktop.",
        "during_session": True,
    },
    {
        "key": "onboarding_done",
        "value": False,
        "is_default": True,
        "explain": "Whether the first-run introduction has been completed.",
        "during_session": True,
    },
]


def view(**overrides: Any) -> Any:
    settings: dict[str, Any] = {"settings": DESCRIBED, "in_session": False}
    settings.update(overrides)
    return settings_view(**settings)


def row(key: str, **overrides: Any) -> Any:
    return {one.key: one for one in view(**overrides).rows}[key]


class TestWhatIsShown:
    def test_the_four_settings_a_person_chose_are_there(self) -> None:
        assert [one.key for one in view().rows] == [
            "firm_wait_seconds",
            "phrase_length",
            "retention_days",
            "language",
        ]

    def test_the_bookkeeping_one_is_not(self) -> None:
        """Whether onboarding ran is Anchor's note to itself, not a setting."""
        assert "onboarding_done" not in [one.key for one in view().rows]

    def test_a_duration_is_written_as_one(self) -> None:
        assert row("firm_wait_seconds").value == "15 min"

    def test_a_count_says_what_it_counts(self) -> None:
        assert row("phrase_length").value == "150 characters"
        assert row("retention_days").value == "90 days"

    def test_the_language_is_named_not_coded(self) -> None:
        assert row("language").value == "Follow the desktop"

    def test_a_chosen_language_is_named(self) -> None:
        chosen = [dict(one, value="es") if one["key"] == "language" else one for one in DESCRIBED]
        assert row("language", settings=chosen).value == "Spanish"

    def test_each_carries_the_engine_s_own_explanation(self) -> None:
        assert "Firm session" in row("firm_wait_seconds").explain

    def test_a_default_is_marked_as_one(self) -> None:
        """So a number nobody chose is not mistaken for one somebody did."""
        assert row("firm_wait_seconds").is_default


class TestDuringASession:
    def test_the_two_exit_settings_are_locked(self) -> None:
        assert row("firm_wait_seconds", in_session=True).locked
        assert row("phrase_length", in_session=True).locked

    def test_and_say_why(self) -> None:
        assert "session" in row("phrase_length", in_session=True).reason

    def test_the_others_are_not(self) -> None:
        assert not row("retention_days", in_session=True).locked
        assert not row("language", in_session=True).locked

    def test_nothing_is_locked_otherwise(self) -> None:
        assert not any(one.locked for one in view().rows)


class TestTheKindOfControl:
    def test_a_language_is_a_list_to_choose_from(self) -> None:
        language = row("language")
        assert language.kind == "choice"
        assert [one.key for one in language.choices] == ["", "en", "es"]

    def test_a_duration_and_a_count_are_numbers(self) -> None:
        assert row("firm_wait_seconds").kind == "duration"
        assert row("retention_days").kind == "number"


class TestDeletingStatistics:
    def test_the_one_action_is_here_too(self) -> None:
        """SPEC 13 puts it in reach; the Statistics page is not the only door."""
        assert view().delete_label
        assert "cannot be undone" in view().delete_warning


class TestSaving:
    def test_a_duration_is_sent_as_seconds_of_text(self) -> None:
        assert set_request("firm_wait_seconds", 1800) == (
            "config.set",
            {"key": "firm_wait_seconds", "value": "1800"},
        )

    def test_a_choice_is_sent_as_it_is(self) -> None:
        _type, payload = set_request("language", "es")
        assert payload == {"key": "language", "value": "es"}

    def test_following_the_desktop_is_sent_as_nothing(self) -> None:
        _type, payload = set_request("language", "")
        assert payload["value"] == ""
