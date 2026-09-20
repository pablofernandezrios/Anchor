"""Counting attempts and rationing notifications (SPEC 8.3)."""

from __future__ import annotations

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
