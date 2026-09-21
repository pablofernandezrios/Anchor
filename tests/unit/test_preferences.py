"""The settings a person may change, and what refuses to change (SPEC 7.2, 13)."""

from __future__ import annotations

import pytest

from anchor.engine.phrases import DEFAULT_PHRASE_LENGTH
from anchor.engine.preferences import (
    PREFERENCES,
    Preferences,
    describe,
    parse_value,
)
from anchor.engine.sessions import DEFAULT_FIRM_WAIT_SECONDS, SessionPolicy
from anchor.protocol.errors import AnchorError, ErrorCode


class TestWhatIsSettable:
    def test_the_spec_s_four_knobs_are_all_here(self) -> None:
        # SPEC 7.2 names the first two, SPEC 13 the third, SPEC 14 the fourth.
        for key in ("firm_wait_seconds", "phrase_length", "retention_days", "language"):
            assert key in PREFERENCES

    def test_an_unknown_key_is_refused_by_name(self) -> None:
        with pytest.raises(AnchorError, match="no setting called 'colour'") as raised:
            parse_value("colour", "blue")
        assert raised.value.code is ErrorCode.INVALID_CONFIG

    def test_the_two_exit_knobs_may_not_change_during_a_session(self) -> None:
        # "Both configurable in settings, never during a session" (SPEC 7.2).
        assert not PREFERENCES["firm_wait_seconds"].during_session
        assert not PREFERENCES["phrase_length"].during_session

    def test_retention_and_language_may(self) -> None:
        assert PREFERENCES["retention_days"].during_session
        assert PREFERENCES["language"].during_session


class TestReadingValues:
    def test_a_number_arrives_as_text_and_leaves_as_a_number(self) -> None:
        assert parse_value("firm_wait_seconds", "900") == 900

    @pytest.mark.parametrize("text", ["", "abc", "9.5", "nine hundred", " "])
    def test_text_that_is_not_a_number_is_refused(self, text: str) -> None:
        with pytest.raises(AnchorError, match="whole number"):
            parse_value("firm_wait_seconds", text)

    def test_a_value_below_the_floor_is_refused_with_the_floor_in_it(self) -> None:
        with pytest.raises(AnchorError, match="at least 60"):
            parse_value("firm_wait_seconds", "30")

    def test_a_value_above_the_ceiling_is_refused(self) -> None:
        with pytest.raises(AnchorError, match="at most"):
            parse_value("phrase_length", "100000")

    @pytest.mark.parametrize(("text", "expected"), [("yes", True), ("no", False), ("1", True)])
    def test_a_flag_reads_the_words_people_type(self, text: str, expected: bool) -> None:
        assert parse_value("onboarding_done", text) is expected

    def test_a_choice_outside_the_list_is_refused_with_the_list_in_it(self) -> None:
        with pytest.raises(AnchorError, match="en, es"):
            parse_value("language", "fr")

    def test_the_empty_language_means_whatever_the_desktop_uses(self) -> None:
        assert parse_value("language", "") == ""


class TestUnsetMeansDefault:
    def test_nothing_is_set_to_begin_with(self) -> None:
        assert Preferences().to_dict() == {}

    def test_the_effective_policy_is_the_engine_s_own_until_something_is_set(self) -> None:
        policy = SessionPolicy(firm_wait_seconds=1, phrase_length=2)
        assert Preferences().policy(policy) == policy

    def test_a_set_value_replaces_it(self) -> None:
        prefs = Preferences().set("firm_wait_seconds", "600")
        policy = prefs.policy(SessionPolicy(firm_wait_seconds=1, phrase_length=2))
        assert policy.firm_wait_seconds == 600
        # And leaves the rest of the policy alone.
        assert policy.phrase_length == 2

    def test_the_default_shown_is_the_engine_s_own_default(self) -> None:
        shown = {entry["key"]: entry for entry in describe(Preferences())}
        assert shown["firm_wait_seconds"]["value"] == DEFAULT_FIRM_WAIT_SECONDS
        assert shown["phrase_length"]["value"] == DEFAULT_PHRASE_LENGTH
        assert shown["firm_wait_seconds"]["is_default"] is True

    def test_a_set_value_stops_being_a_default(self) -> None:
        prefs = Preferences().set("retention_days", "30")
        shown = {entry["key"]: entry for entry in describe(prefs)}
        assert shown["retention_days"] == {
            "key": "retention_days",
            "value": 30,
            "is_default": False,
            "explain": PREFERENCES["retention_days"].explain,
            "during_session": True,
        }

    def test_retention_falls_back_to_the_install_setting(self) -> None:
        assert Preferences().retention(installed=45) == 45
        assert Preferences().set("retention_days", "7").retention(installed=45) == 7


class TestStorage:
    def test_what_was_set_survives_a_round_trip(self) -> None:
        prefs = Preferences().set("language", "es").set("phrase_length", "200")
        assert Preferences.from_dict(prefs.to_dict()) == prefs

    def test_only_what_was_set_is_written_down(self) -> None:
        assert Preferences().set("language", "es").to_dict() == {"language": "es"}

    def test_a_stored_value_that_makes_no_sense_falls_back_to_the_default(self) -> None:
        # A hand-edited config.json must not stop the engine from starting.
        prefs = Preferences.from_dict({"firm_wait_seconds": "banana", "language": "es"})
        assert prefs.firm_wait_seconds is None
        assert prefs.language == "es"

    def test_a_stored_key_nobody_recognises_is_dropped(self) -> None:
        assert Preferences.from_dict({"colour": "blue"}) == Preferences()
