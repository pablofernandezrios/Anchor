"""The short-lived map of recent answers (SPEC 8.2)."""

from __future__ import annotations

import pytest

from anchor.blocker.recent import RecentAnswers


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def recent(clock: Clock) -> RecentAnswers:
    return RecentAnswers(ttl=600.0, max_entries=5, clock=clock)


def blocks(*names: str):  # type: ignore[no-untyped-def]
    return lambda name: name in names


class TestRemembering:
    def test_addresses_come_back_for_a_blocked_name(self, recent: RecentAnswers) -> None:
        recent.record("youtube.com", ["142.250.1.1", "142.250.1.2"])

        assert recent.addresses_for(blocks("youtube.com")) == ["142.250.1.1", "142.250.1.2"]

    def test_an_allowed_name_contributes_nothing(self, recent: RecentAnswers) -> None:
        recent.record("example.com", ["93.184.216.34"])
        assert recent.addresses_for(blocks("youtube.com")) == []

    def test_an_answer_with_no_addresses_is_not_stored(self, recent: RecentAnswers) -> None:
        recent.record("youtube.com", [])
        assert len(recent) == 0

    def test_the_same_address_is_only_listed_once(self, recent: RecentAnswers) -> None:
        """Two names on a shared host must not mean two identical rules."""
        recent.record("youtube.com", ["142.250.1.1"])
        recent.record("m.youtube.com", ["142.250.1.1"])

        assert recent.addresses_for(blocks("youtube.com", "m.youtube.com")) == ["142.250.1.1"]

    def test_a_later_answer_replaces_an_earlier_one(self, recent: RecentAnswers) -> None:
        recent.record("youtube.com", ["1.1.1.1"])
        recent.record("youtube.com", ["2.2.2.2"])

        assert recent.addresses_for(blocks("youtube.com")) == ["2.2.2.2"]


class TestForgetting:
    def test_an_old_answer_is_ignored(self, recent: RecentAnswers, clock: Clock) -> None:
        recent.record("youtube.com", ["1.1.1.1"])
        clock.advance(601)

        assert recent.addresses_for(blocks("youtube.com")) == []

    def test_an_answer_inside_the_window_still_counts(
        self, recent: RecentAnswers, clock: Clock
    ) -> None:
        recent.record("youtube.com", ["1.1.1.1"])
        clock.advance(599)

        assert recent.addresses_for(blocks("youtube.com")) == ["1.1.1.1"]

    def test_pruning_drops_the_expired(self, recent: RecentAnswers, clock: Clock) -> None:
        recent.record("old.com", ["1.1.1.1"])
        clock.advance(601)
        recent.record("new.com", ["2.2.2.2"])

        recent.prune()
        assert len(recent) == 1

    def test_the_map_is_bounded(self, recent: RecentAnswers) -> None:
        """A machine resolving a lot must not grow this without end."""
        for index in range(20):
            recent.record(f"site{index}.com", [f"10.0.0.{index}"])

        assert len(recent) == 5

    def test_the_oldest_goes_first(self, recent: RecentAnswers) -> None:
        for index in range(7):
            recent.record(f"site{index}.com", [f"10.0.0.{index}"])

        assert recent.addresses_for(blocks("site0.com")) == []
        assert recent.addresses_for(blocks("site6.com")) == ["10.0.0.6"]

    def test_clearing_forgets_everything(self, recent: RecentAnswers) -> None:
        """A session ending should not leave a record of it behind."""
        recent.record("youtube.com", ["1.1.1.1"])
        recent.clear()

        assert len(recent) == 0
        assert recent.addresses_for(blocks("youtube.com")) == []
