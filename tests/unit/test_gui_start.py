"""Start Session and its confirmation (SPEC 7.1, mockups 2 and 3)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from anchor.gui.start import (
    DURATION_STEP,
    MAX_MANUAL,
    StartForm,
    confirmation,
    request_for,
    start_form,
    stepped,
)

#: Monday 21 September 2026, 09:30 local time.
NOW = datetime(2026, 9, 21, 9, 30).timestamp()


def profile(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "Study",
        "web_mode": "blocklist",
        "categories": ["games", "news", "social", "video"],
        "domains": ["news.ycombinator.com", "twitch.tv"],
        "apps": ["discord.desktop", "steam.desktop"],
        "breaks": {
            "work_minutes": 50,
            "break_minutes": 10,
            "type": "overlay",
            "hardness": "moderate",
            "long_break_every": None,
            "long_break_minutes": None,
            "allow_sites_during_breaks": False,
            "warning_seconds": 60,
        },
        "block_vpn_and_tor": True,
    }
    base.update(overrides)
    return base


def form(**overrides: Any) -> StartForm:
    settings: dict[str, Any] = {
        "profiles": ["Study", "Work"],
        "profile": profile(),
        "duration_seconds": 9000,
        "level": "firm",
        "valve": None,
        "now": NOW,
        "apps": {"discord.desktop": "Discord", "steam.desktop": "Steam"},
    }
    settings.update(overrides)
    return start_form(**settings)


class TestTheProfile:
    def test_every_profile_can_be_chosen(self) -> None:
        assert form().profiles == ("Study", "Work")

    def test_what_it_blocks_is_summed_up_as_the_mockup_does(self) -> None:
        assert form().blocks == "Blocklist · 4 categories · 2 domains · 2 apps"

    def test_an_allowlist_is_named_as_one(self) -> None:
        assert form(profile=profile(web_mode="allowlist")).blocks.startswith("Allowlist")

    def test_one_of_each_is_singular(self) -> None:
        summed = form(profile=profile(categories=["social"], domains=["x.com"], apps=[])).blocks
        assert summed == "Blocklist · 1 category · 1 domain · 0 apps"


class TestTheDuration:
    def test_it_reads_the_way_the_interface_writes_durations(self) -> None:
        assert form().duration == "2 h 30 min"

    def test_it_says_when_the_session_would_end(self) -> None:
        assert form().ends == "Ends today at 12:00"

    def test_a_session_running_past_midnight_says_tomorrow(self) -> None:
        late = datetime(2026, 9, 21, 23, 0).timestamp()
        assert form(now=late, duration_seconds=7200).ends == "Ends tomorrow at 01:00"

    def test_the_eight_hour_cap_is_stated(self) -> None:
        assert form().cap == "Maximum at start: 8 h"

    @pytest.mark.parametrize(
        ("start", "step", "expected"),
        [
            (9000, +1, 9000 + DURATION_STEP),
            (9000, -1, 9000 - DURATION_STEP),
            (DURATION_STEP, -1, DURATION_STEP),
            (MAX_MANUAL, +1, MAX_MANUAL),
        ],
    )
    def test_the_buttons_move_it_and_stop_at_the_ends(
        self, start: int, step: int, expected: int
    ) -> None:
        assert stepped(start, step) == expected

    def test_a_duration_over_the_cap_is_refused_before_the_engine_sees_it(self) -> None:
        """SPEC 7.1: 8 hours at start. Saying so here beats a refusal later."""
        over = form(duration_seconds=MAX_MANUAL + 60)
        assert not over.can_start
        assert "8 h" in over.problem


class TestTheLevels:
    def test_all_three_are_offered_with_what_they_cost(self) -> None:
        levels = form().levels
        assert [choice.key for choice in levels] == ["soft", "firm", "strict"]
        assert "5 min" in levels[0].detail
        assert "only add" in levels[1].detail.lower()
        assert "VPN" in levels[2].detail

    def test_the_chosen_one_is_remembered(self) -> None:
        assert form().level == "firm"


class TestTheValve:
    def test_it_is_only_asked_for_in_strict(self) -> None:
        assert form().valves == ()

    def test_strict_offers_all_three(self) -> None:
        strict = form(level="strict")
        assert [choice.key for choice in strict.valves] == ["wait", "phrase", "both"]

    def test_the_wait_says_it_can_be_withdrawn(self) -> None:
        """SPEC 7.5, and the mockup's own words."""
        strict = form(level="strict")
        assert "withdraw" in strict.valves[0].detail

    def test_strict_without_a_valve_cannot_start_yet(self) -> None:
        strict = form(level="strict")
        assert not strict.can_start
        assert "valve" in strict.problem

    def test_strict_with_one_can(self) -> None:
        assert form(level="strict", valve="phrase").can_start

    def test_a_valve_chosen_below_strict_is_dropped(self) -> None:
        """It has no meaning there, and would be sent to an engine that refuses it."""
        assert request_for(form(level="soft", valve="wait"))[1].get("valve") is None


class TestTheBreaks:
    def test_the_pattern_is_shown_but_not_editable_here(self) -> None:
        shown = form()
        assert shown.breaks == "50/10 · Moderate · Fullscreen"
        assert "profile" in shown.breaks_hint

    def test_a_long_break_is_part_of_the_pattern(self) -> None:
        long = form(
            profile=profile(
                breaks={**profile()["breaks"], "long_break_every": 4, "long_break_minutes": 20}
            )
        )
        assert "20 min every 4" in long.breaks


class TestTheRequest:
    def test_it_is_what_the_engine_expects(self) -> None:
        assert request_for(form()) == (
            "session.start",
            {"profile": "Study", "duration_seconds": 9000, "level": "firm"},
        )

    def test_a_strict_session_carries_its_valve(self) -> None:
        _type, payload = request_for(form(level="strict", valve="both"))
        assert payload["valve"] == "both"


class TestTheConfirmation:
    """SPEC 7.1: the dialog lists the consequences (mockup 3)."""

    def test_it_names_the_level_in_the_question(self) -> None:
        assert confirmation(form(level="strict", valve="phrase")).title == "Start a Strict session?"

    def test_it_repeats_the_profile_and_the_length(self) -> None:
        assert confirmation(form()).profile_line == "Study profile · 2 h 30 min"

    def test_it_says_the_end_time_and_that_it_can_only_grow(self) -> None:
        said = confirmation(form()).ends_line
        assert "12:00" in said
        assert "never shorten" in said

    def test_a_strict_session_says_there_is_no_cancelling(self) -> None:
        said = confirmation(form(level="strict", valve="phrase")).exit_line
        assert "cannot be cancelled" in said
        assert "random phrase" in said

    def test_a_soft_session_says_what_cancelling_costs(self) -> None:
        assert "5 minutes" in confirmation(form(level="soft")).exit_line

    def test_strict_warns_about_vpn_and_tor(self) -> None:
        assert "VPN and Tor" in confirmation(form(level="strict", valve="wait")).tunnels_line

    def test_a_profile_that_keeps_them_says_nothing_about_them(self) -> None:
        """ADR 4 lets a profile opt out, and a warning that is not true is worse."""
        kept = form(level="strict", valve="wait", profile=profile(block_vpn_and_tor=False))
        assert confirmation(kept).tunnels_line == ""

    def test_nothing_below_strict_mentions_them(self) -> None:
        assert confirmation(form()).tunnels_line == ""

    def test_it_names_the_applications_that_will_close(self) -> None:
        said = confirmation(form()).apps_line
        assert "Discord and Steam" in said
        assert "2 minutes" in said

    def test_an_application_with_no_desktop_entry_is_named_by_its_identifier(self) -> None:
        said = confirmation(form(apps={})).apps_line
        assert "discord.desktop" in said

    def test_a_profile_that_closes_nothing_says_nothing(self) -> None:
        assert confirmation(form(profile=profile(apps=[]))).apps_line == ""

    def test_an_allowlist_warns_that_the_web_will_mostly_stop_working(self) -> None:
        """SPEC 8.1 asks for a clear warning; the moment for it is here."""
        said = confirmation(form(profile=profile(web_mode="allowlist"))).web_line
        assert "everything except" in said
