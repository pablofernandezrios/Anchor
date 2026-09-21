"""Weekly schedules: when they run, and what overlapping ones add up to (SPEC 11)."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from anchor.engine.profiles import Profile
from anchor.engine.schedules import (
    Occurrence,
    Schedule,
    active_at,
    format_clock,
    merge,
    occurrence_on,
    parse_clock,
    parse_day,
)
from anchor.protocol.errors import AnchorError
from anchor.protocol.types import Level, Valve, WebMode

MONDAY = date(2026, 9, 21)
WEDNESDAY = date(2026, 9, 23)


def at(day: date, hour: int, minute: int = 0) -> float:
    return datetime(day.year, day.month, day.day, hour, minute).timestamp()


def schedule(**overrides: object) -> Schedule:
    base: dict[str, object] = {
        "id": "s1",
        "name": "Study",
        "profile": "Study",
        "days": frozenset({0, 2, 4}),  # Monday, Wednesday, Friday
        "start_minute": 9 * 60,
        "end_minute": 13 * 60,
        "level": Level.FIRM,
    }
    base.update(overrides)
    return Schedule(**base)  # type: ignore[arg-type]


class TestReadingTimes:
    @pytest.mark.parametrize(("text", "minutes"), [("09:30", 570), ("00:00", 0), ("23:59", 1439)])
    def test_a_clock_time_becomes_minutes(self, text: str, minutes: int) -> None:
        assert parse_clock(text) == minutes

    @pytest.mark.parametrize("text", ["9:30am", "24:00", "09:60", "nine", "0930", ""])
    def test_rubbish_is_refused(self, text: str) -> None:
        with pytest.raises(ValueError, match="time of day"):
            parse_clock(text)

    def test_minutes_become_a_clock_time_again(self) -> None:
        assert format_clock(570) == "09:30"

    def test_days_can_be_named_or_numbered(self) -> None:
        assert parse_day("monday") == 0
        assert parse_day("Sunday") == 6
        assert parse_day(3) == 3

    def test_a_day_that_is_not_a_day_is_refused(self) -> None:
        with pytest.raises(ValueError, match="day of the week"):
            parse_day("caturday")


class TestTheModel:
    def test_it_round_trips_through_the_configuration(self) -> None:
        original = schedule(valve=None)

        assert Schedule.from_dict(original.to_dict()) == original

    def test_a_strict_schedule_needs_a_valve(self) -> None:
        """SPEC 11: chosen with the schedule, not in the moment it starts."""
        with pytest.raises(AnchorError, match="valve"):
            schedule(level=Level.STRICT, valve=None)

    def test_a_strict_schedule_with_a_valve_is_fine(self) -> None:
        assert schedule(level=Level.STRICT, valve=Valve.PHRASE).valve is Valve.PHRASE

    def test_a_schedule_needs_a_day(self) -> None:
        with pytest.raises(ValueError, match="at least one day"):
            schedule(days=frozenset())

    def test_a_schedule_needs_a_name(self) -> None:
        with pytest.raises(ValueError, match="needs a name"):
            schedule(name="  ")

    def test_an_empty_window_is_refused(self) -> None:
        with pytest.raises(ValueError, match="cannot be empty"):
            schedule(start_minute=540, end_minute=540)

    def test_an_ordinary_window_knows_its_length(self) -> None:
        assert schedule().length_minutes == 4 * 60

    def test_a_window_over_midnight_knows_its_length(self) -> None:
        night = schedule(start_minute=22 * 60, end_minute=2 * 60)

        assert night.crosses_midnight
        assert night.length_minutes == 4 * 60


class TestWhenTheyRun:
    def test_inside_the_window_on_one_of_its_days(self) -> None:
        assert active_at([schedule()], at(WEDNESDAY, 10)) != []

    def test_not_before_it_opens(self) -> None:
        assert active_at([schedule()], at(WEDNESDAY, 8, 59)) == []

    def test_the_first_minute_counts(self) -> None:
        assert active_at([schedule()], at(WEDNESDAY, 9)) != []

    def test_the_last_minute_does_not(self) -> None:
        """13:00 is when it ends, so at 13:00 it has ended."""
        assert active_at([schedule()], at(WEDNESDAY, 13)) == []

    def test_not_on_a_day_it_does_not_name(self) -> None:
        assert active_at([schedule()], at(date(2026, 9, 22), 10)) == []

    def test_a_disabled_schedule_never_runs(self) -> None:
        assert active_at([schedule(enabled=False)], at(WEDNESDAY, 10)) == []

    def test_a_window_over_midnight_runs_after_midnight(self) -> None:
        """A schedule for Monday 22:00 is still running at 01:00 on Tuesday."""
        night = schedule(days=frozenset({0}), start_minute=22 * 60, end_minute=2 * 60)

        assert active_at([night], at(date(2026, 9, 22), 1)) != []

    def test_and_not_on_the_following_night(self) -> None:
        night = schedule(days=frozenset({0}), start_minute=22 * 60, end_minute=2 * 60)

        assert active_at([night], at(date(2026, 9, 22), 23)) == []

    def test_an_occurrence_knows_when_it_ends(self) -> None:
        occurrence = occurrence_on(schedule(), WEDNESDAY)

        assert occurrence.ends_at == at(WEDNESDAY, 13)
        assert occurrence.remaining(at(WEDNESDAY, 12)) == 3600

    def test_remaining_never_goes_negative(self) -> None:
        assert occurrence_on(schedule(), WEDNESDAY).remaining(at(WEDNESDAY, 23)) == 0


class TestMergingOverlaps:
    """SPEC 11: rules are merged and the strictest level wins."""

    def profiles(self) -> dict[str, Profile]:
        return {
            "Study": Profile(
                name="Study",
                domains=frozenset({"youtube.com"}),
                apps=frozenset({"discord.desktop"}),
                categories=frozenset({"social"}),
            ),
            "Work": Profile(
                name="Work",
                domains=frozenset({"reddit.com"}),
                apps=frozenset({"steam.desktop"}),
                categories=frozenset({"games"}),
            ),
            "Locked": Profile(
                name="Locked",
                web_mode=WebMode.ALLOWLIST,
                domains=frozenset({"wikipedia.org", "university.example"}),
            ),
            "AlsoLocked": Profile(
                name="AlsoLocked",
                web_mode=WebMode.ALLOWLIST,
                domains=frozenset({"university.example", "docs.example"}),
            ),
        }

    def overlapping(self, *schedules: Schedule) -> list[Occurrence]:
        return [occurrence_on(item, WEDNESDAY) for item in schedules]

    def test_one_schedule_is_itself(self) -> None:
        merged = merge(self.overlapping(schedule()), self.profiles())

        assert merged is not None
        assert merged.profile.name == "Study"
        assert merged.level is Level.FIRM

    def test_the_strictest_level_wins(self) -> None:
        merged = merge(
            self.overlapping(
                schedule(level=Level.SOFT),
                schedule(id="s2", profile="Work", level=Level.STRICT, valve=Valve.WAIT),
            ),
            self.profiles(),
        )

        assert merged is not None
        assert merged.level is Level.STRICT

    def test_and_brings_its_own_valve(self) -> None:
        merged = merge(
            self.overlapping(
                schedule(level=Level.SOFT),
                schedule(id="s2", profile="Work", level=Level.STRICT, valve=Valve.BOTH),
            ),
            self.profiles(),
        )

        assert merged is not None
        assert merged.valve is Valve.BOTH

    def test_blocked_domains_are_the_union(self) -> None:
        merged = merge(
            self.overlapping(schedule(), schedule(id="s2", profile="Work")),
            self.profiles(),
        )

        assert merged is not None
        assert merged.profile.domains == frozenset({"youtube.com", "reddit.com"})

    def test_so_are_applications_and_categories(self) -> None:
        merged = merge(
            self.overlapping(schedule(), schedule(id="s2", profile="Work")),
            self.profiles(),
        )

        assert merged is not None
        assert merged.profile.apps == frozenset({"discord.desktop", "steam.desktop"})
        assert merged.profile.categories == frozenset({"social", "games"})

    def test_an_allowlist_beats_a_blocklist(self) -> None:
        """It blocks everything it does not name, which is stricter."""
        merged = merge(
            self.overlapping(schedule(), schedule(id="s2", profile="Locked")),
            self.profiles(),
        )

        assert merged is not None
        assert merged.profile.web_mode is WebMode.ALLOWLIST

    def test_two_allowlists_keep_only_what_both_allow(self) -> None:
        merged = merge(
            self.overlapping(schedule(profile="Locked"), schedule(id="s2", profile="AlsoLocked")),
            self.profiles(),
        )

        assert merged is not None
        assert merged.profile.domains == frozenset({"university.example"})

    def test_a_blocklist_takes_its_domains_out_of_an_allowlist(self) -> None:
        """Otherwise overlapping two schedules would unblock something."""
        profiles = self.profiles()
        profiles["Blocks"] = Profile(name="Blocks", domains=frozenset({"wikipedia.org"}))

        merged = merge(
            self.overlapping(schedule(profile="Locked"), schedule(id="s2", profile="Blocks")),
            profiles,
        )

        assert merged is not None
        assert "wikipedia.org" not in merged.profile.domains
        assert "university.example" in merged.profile.domains

    def test_the_session_ends_when_the_last_one_does(self) -> None:
        """A schedule ending earlier does not cut the others short."""
        merged = merge(
            self.overlapping(
                schedule(),
                schedule(id="s2", profile="Work", start_minute=12 * 60, end_minute=16 * 60),
            ),
            self.profiles(),
        )

        assert merged is not None
        assert merged.ends_at == at(WEDNESDAY, 16)

    def test_one_schedule_asking_for_tunnels_is_enough(self) -> None:
        profiles = self.profiles()
        profiles["Open"] = Profile(name="Open", block_vpn_and_tor=False)

        merged = merge(
            self.overlapping(schedule(), schedule(id="s2", profile="Open")),
            profiles,
        )

        assert merged is not None
        assert merged.profile.block_vpn_and_tor is True

    def test_nothing_active_merges_to_nothing(self) -> None:
        assert merge([], self.profiles()) is None

    def test_a_profile_that_is_not_installed_blocks_everything(self) -> None:
        """Blocking nothing would be the wrong way to notice it is missing."""
        merged = merge(self.overlapping(schedule(profile="Gone")), {})

        assert merged is not None
        assert merged.profile.web_mode is WebMode.ALLOWLIST
        assert merged.profile.domains == frozenset()

    def test_the_schedules_that_made_it_are_named(self) -> None:
        merged = merge(
            self.overlapping(schedule(), schedule(id="s2", profile="Work")),
            self.profiles(),
        )

        assert merged is not None
        assert set(merged.schedule_ids) == {"s1", "s2"}
