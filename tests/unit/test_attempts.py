"""Counting attempts and rationing notifications (SPEC 8.3)."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from anchor.blocker.attempts import AttemptTracker


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def tracker(clock: Clock) -> AttemptTracker:
    return AttemptTracker(notify_interval=600.0, attempt_window=5.0, clock=clock)


class TestCountingAttempts:
    def test_one_visit_is_one_attempt(self, tracker: AttemptTracker, clock: Clock) -> None:
        """Resolving a name asks for A and AAAA, which is still one visit."""
        first = tracker.record("youtube.com")
        clock.advance(0.01)
        second = tracker.record("youtube.com")

        assert first.counted
        assert not second.counted
        assert tracker.total == 1

    def test_a_browser_retrying_is_still_one_attempt(
        self, tracker: AttemptTracker, clock: Clock
    ) -> None:
        for _ in range(6):
            clock.advance(0.2)
            tracker.record("youtube.com")

        assert tracker.total == 1

    def test_trying_again_later_is_a_new_attempt(
        self, tracker: AttemptTracker, clock: Clock
    ) -> None:
        tracker.record("youtube.com")
        clock.advance(5.0)

        assert tracker.record("youtube.com").counted
        assert tracker.total == 2

    def test_different_domains_count_separately(self, tracker: AttemptTracker) -> None:
        tracker.record("youtube.com")
        tracker.record("reddit.com")

        assert tracker.total == 2

    def test_a_subdomain_counts_on_its_own(self, tracker: AttemptTracker) -> None:
        """The user reached for two different addresses, even if one rule caught both."""
        tracker.record("youtube.com")
        tracker.record("m.youtube.com")

        assert tracker.total == 2


class TestRationingNotifications:
    def test_the_first_attempt_is_announced(self, tracker: AttemptTracker) -> None:
        assert tracker.record("youtube.com").notify

    def test_the_same_domain_is_not_announced_again_soon(
        self, tracker: AttemptTracker, clock: Clock
    ) -> None:
        tracker.record("youtube.com")

        clock.advance(60)
        assert not tracker.record("youtube.com").notify

        clock.advance(300)
        assert not tracker.record("youtube.com").notify

    def test_after_ten_minutes_it_is_announced_again(
        self, tracker: AttemptTracker, clock: Clock
    ) -> None:
        """SPEC 8.3: at most one per domain every 10 minutes."""
        tracker.record("youtube.com")
        clock.advance(600)

        assert tracker.record("youtube.com").notify

    def test_each_domain_has_its_own_allowance(self, tracker: AttemptTracker) -> None:
        assert tracker.record("youtube.com").notify
        assert tracker.record("reddit.com").notify

    def test_an_uncounted_query_never_notifies(self, tracker: AttemptTracker, clock: Clock) -> None:
        """The AAAA half of a visit must not produce a second notification."""
        tracker.record("youtube.com")
        clock.advance(0.01)

        assert not tracker.record("youtube.com").notify

    def test_a_storm_produces_one_notification(self, tracker: AttemptTracker, clock: Clock) -> None:
        announced = 0
        for _ in range(200):
            clock.advance(1.0)
            if tracker.record("youtube.com").notify:
                announced += 1

        # 200 seconds of hammering, well inside one ten-minute window.
        assert announced == 1


class TestHousekeeping:
    def test_the_tracker_is_bounded(self, clock: Clock) -> None:
        tracker = AttemptTracker(max_tracked=10, clock=clock)
        for index in range(50):
            tracker.record(f"site{index}.com")

        assert tracker.total == 50  # the count is not forgotten

    def test_resetting_clears_everything(self, tracker: AttemptTracker) -> None:
        tracker.record("youtube.com")
        tracker.reset()

        assert tracker.total == 0
        assert tracker.record("youtube.com").notify


class TestTheSessionWideLimit:
    """The per-domain rule is necessary and, alone, badly insufficient.

    An allowlist session blocks every name the machine reaches for, which on
    an idle desktop is telemetry, update checks and a browser's own services:
    hundreds of distinct names nobody typed. The first allowlist session on a
    real desktop produced roughly four hundred notifications in five minutes,
    each one obeying the per-domain rule perfectly, and left the machine
    crawling.
    """

    def tracker(self, clock: Callable[[], float]) -> AttemptTracker:
        return AttemptTracker(budget=3, budget_window=60.0, clock=clock)

    def test_a_flood_of_new_domains_is_capped(self) -> None:
        now = [0.0]
        tracker = self.tracker(lambda: now[0])

        told = 0
        for i in range(400):
            now[0] += 0.01
            if tracker.record(f"host{i}.example.com").notify:
                told += 1

        assert told == 3, "every new domain was announced"

    def test_and_the_ones_held_back_are_counted(self) -> None:
        now = [0.0]
        tracker = self.tracker(lambda: now[0])
        for i in range(50):
            now[0] += 0.01
            tracker.record(f"host{i}.example.com")

        now[0] += 61.0
        outcome = tracker.record("late.example.com")

        assert outcome.notify
        assert outcome.withheld == 47, "the flood was silent and left no trace"

    def test_the_budget_returns_with_the_next_window(self) -> None:
        now = [0.0]
        tracker = self.tracker(lambda: now[0])
        for i in range(10):
            now[0] += 0.01
            tracker.record(f"host{i}.example.com")

        now[0] += 61.0
        assert tracker.record("a.example.com").notify

    def test_a_quiet_session_still_says_everything(self) -> None:
        """One block every few minutes is not a flood, and is named."""
        now = [0.0]
        tracker = self.tracker(lambda: now[0])

        for i in range(10):
            now[0] += 120.0
            assert tracker.record(f"host{i}.example.com").notify

    def test_nothing_is_lost_from_the_count(self) -> None:
        """Held back from the notifications, never from the statistics."""
        now = [0.0]
        tracker = self.tracker(lambda: now[0])
        for i in range(400):
            now[0] += 0.01
            tracker.record(f"host{i}.example.com")

        assert tracker.total == 400
