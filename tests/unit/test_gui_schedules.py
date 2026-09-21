"""The Schedules screen and its week grid (SPEC 11, mockup 5)."""

from __future__ import annotations

from typing import Any

from anchor.gui.schedules import create_request, edit_request, schedule_grid


def entry(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "s1",
        "name": "Study",
        "profile": "Study",
        "days": [0, 2, 4],
        "start": "09:00",
        "end": "13:00",
        "level": "firm",
        "valve": None,
        "enabled": True,
        "active": False,
    }
    base.update(overrides)
    return base


def listing(*entries: dict[str, Any], skips: int = 1) -> dict[str, Any]:
    return {"schedules": list(entries) or [entry()], "skips_remaining": skips}


def grid(**overrides: Any) -> Any:
    settings: dict[str, Any] = {
        "listing": listing(),
        "status": {"active": False, "skips_remaining": 1},
        "today": 4,  # Friday, as the mockup marks it
    }
    settings.update(overrides)
    return schedule_grid(**settings)


class TestTheWeek:
    def test_it_is_seven_days_starting_on_monday(self) -> None:
        assert [day.name for day in grid().days] == [
            "Mon",
            "Tue",
            "Wed",
            "Thu",
            "Fri",
            "Sat",
            "Sun",
        ]

    def test_today_is_marked(self) -> None:
        assert [day.today for day in grid().days] == [False] * 4 + [True, False, False]

    def test_a_schedule_appears_on_each_of_its_days(self) -> None:
        placed = {day.index for day in grid().days if day.blocks}
        assert placed == {0, 2, 4}

    def test_a_block_says_what_it_is(self) -> None:
        block = grid().days[0].blocks[0]
        assert block.name == "Study"
        assert block.level == "firm"
        assert block.window == "9–13"

    def test_a_window_with_minutes_keeps_them(self) -> None:
        odd = grid(listing=listing(entry(start="09:30", end="13:15")))
        assert odd.days[0].blocks[0].window == "9:30–13:15"


class TestTheTimeAxis:
    def test_it_covers_the_schedules_that_exist(self) -> None:
        wide = grid(
            listing=listing(
                entry(start="09:00", end="13:00"),
                entry(id="s2", days=[0], start="16:00", end="19:00"),
            )
        )
        assert wide.first_minute == 9 * 60
        assert wide.last_minute == 19 * 60

    def test_it_has_an_hour_label_for_each_hour(self) -> None:
        assert grid().hours == ("09:00", "10:00", "11:00", "12:00", "13:00")

    def test_an_empty_week_still_has_a_working_day_on_it(self) -> None:
        """An axis of zero height would draw nothing at all."""
        empty = grid(listing={"schedules": [], "skips_remaining": 3})
        assert empty.first_minute < empty.last_minute

    def test_a_block_knows_where_it_sits_in_the_axis(self) -> None:
        block = (
            grid(
                listing=listing(
                    entry(start="09:00", end="13:00"),
                    entry(id="s2", days=[0], start="16:00", end="19:00"),
                )
            )
            .days[0]
            .blocks[0]
        )
        assert block.offset == 0.0
        assert block.extent == (4 * 60) / (10 * 60)


class TestWindowsThatCrossMidnight:
    """SPEC 11 allows them, and a grid drawn naively loses half of one."""

    def test_the_block_is_split_across_two_days(self) -> None:
        late = grid(listing=listing(entry(days=[0], start="22:00", end="02:00")))
        assert [len(day.blocks) for day in late.days] == [1, 1, 0, 0, 0, 0, 0]

    def test_the_first_half_runs_to_midnight(self) -> None:
        late = grid(listing=listing(entry(days=[0], start="22:00", end="02:00")))
        monday = late.days[0].blocks[0]
        assert (monday.from_minute, monday.to_minute) == (22 * 60, 24 * 60)

    def test_the_second_half_starts_at_midnight_on_the_next_day(self) -> None:
        late = grid(listing=listing(entry(days=[0], start="22:00", end="02:00")))
        tuesday = late.days[1].blocks[0]
        assert (tuesday.from_minute, tuesday.to_minute) == (0, 2 * 60)

    def test_a_sunday_window_wraps_round_to_monday(self) -> None:
        late = grid(listing=listing(entry(days=[6], start="23:00", end="01:00")))
        assert [len(day.blocks) for day in late.days] == [1, 0, 0, 0, 0, 0, 1]

    def test_both_halves_say_the_whole_window(self) -> None:
        """So neither half reads as a schedule that stops at midnight."""
        late = grid(listing=listing(entry(days=[0], start="22:00", end="02:00")))
        assert late.days[1].blocks[0].window == "22–2"


class TestWhatCanBeDone:
    def test_an_inactive_schedule_can_be_edited_and_deleted(self) -> None:
        block = grid().days[0].blocks[0]
        assert block.editable
        assert not block.reason

    def test_the_one_running_now_cannot(self) -> None:
        running = grid(listing=listing(entry(active=True)))
        block = running.days[0].blocks[0]
        assert not block.editable
        assert "running now" in block.reason

    def test_the_note_says_the_rule_out_loud(self) -> None:
        assert "edited freely" in grid().note


class TestSkipping:
    def test_it_is_offered_while_a_scheduled_session_runs(self) -> None:
        skipping = grid(
            status={"active": True, "origin": "schedule", "level": "firm", "skips_remaining": 2}
        )
        assert skipping.can_skip
        assert "2 left" in skipping.skip_label

    def test_it_is_not_offered_when_nothing_is_running(self) -> None:
        assert not grid().can_skip

    def test_a_manual_session_cannot_be_skipped(self) -> None:
        manual = grid(
            status={"active": True, "origin": "manual", "level": "soft", "skips_remaining": 2}
        )
        assert not manual.can_skip
        assert "started by hand" in manual.skip_reason

    def test_a_strict_schedule_cannot_be_skipped(self) -> None:
        strict = grid(
            status={"active": True, "origin": "schedule", "level": "strict", "skips_remaining": 2}
        )
        assert not strict.can_skip
        assert "valve" in strict.skip_reason

    def test_with_none_left_it_says_when_they_come_back(self) -> None:
        spent = grid(
            status={"active": True, "origin": "schedule", "level": "firm", "skips_remaining": 0}
        )
        assert not spent.can_skip
        assert "Monday" in spent.skip_reason

    def test_the_count_is_on_the_screen_either_way(self) -> None:
        assert grid().skips == "Skips this week: 2 of 3"


class TestSavingASchedule:
    def test_a_new_one_carries_everything_the_engine_needs(self) -> None:
        assert create_request(
            name="Reading",
            profile="Reading",
            days=[0, 6],
            start="10:00",
            end="12:00",
            level="soft",
            valve=None,
        ) == (
            "schedule.create",
            {
                "name": "Reading",
                "profile": "Reading",
                "days": ["monday", "sunday"],
                "start": "10:00",
                "end": "12:00",
                "level": "soft",
            },
        )

    def test_a_strict_one_carries_its_valve(self) -> None:
        _type, payload = create_request(
            name="Work",
            profile="Work",
            days=[0],
            start="16:00",
            end="19:00",
            level="strict",
            valve="wait",
        )
        assert payload["valve"] == "wait"

    def test_an_unset_valve_is_left_out_rather_than_sent_as_nothing(self) -> None:
        """The schema refuses a key that is present and empty."""
        _type, payload = create_request(
            name="Work", profile="Work", days=[0], start="16:00", end="19:00", level="soft"
        )
        assert "valve" not in payload

    def test_an_edit_sends_only_what_moved(self) -> None:
        after = entry(start="10:00")
        assert edit_request(entry(), after) == (
            "schedule.edit",
            {"id": "s1", "start": "10:00"},
        )

    def test_changing_the_days_sends_them_by_name(self) -> None:
        _type, payload = edit_request(entry(), entry(days=[1, 3]))  # type: ignore[misc]
        assert payload["days"] == ["tuesday", "thursday"]

    def test_nothing_changed_sends_nothing(self) -> None:
        assert edit_request(entry(), entry()) is None

    def test_turning_one_off_is_a_change(self) -> None:
        _type, payload = edit_request(entry(), entry(enabled=False))  # type: ignore[misc]
        assert payload["enabled"] is False
