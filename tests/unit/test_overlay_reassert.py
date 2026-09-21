"""Asking the compositor to bring the break back (ADR 2).

The overlay itself needs a display, so it cannot run here. This one decision
can, and it is the one that was wrong: asking once when focus is lost is not
"the overlay reasserts itself", it is a single request a compositor is free to
ignore, after which Anchor says nothing for the rest of the break.
"""

from __future__ import annotations

import pytest

from anchor.agent.overlay import REPRESENT_SECONDS, should_reassert


class TestWhileTheBreakRuns:
    def test_a_window_that_lost_focus_is_asked_for_again(self) -> None:
        assert should_reassert(showing=True, active=False, since_last=REPRESENT_SECONDS)

    def test_and_again_later(self) -> None:
        """The whole point: once is not reasserting."""
        assert should_reassert(showing=True, active=False, since_last=60.0)

    def test_the_focused_window_is_left_alone(self) -> None:
        """Presenting a window that already has focus is noise."""
        assert not should_reassert(showing=True, active=True, since_last=60.0)

    def test_it_is_not_asked_twice_in_a_moment(self) -> None:
        """A compositor that refuses causes the focus change that asks again."""
        assert not should_reassert(showing=True, active=False, since_last=0.1)

    @pytest.mark.parametrize("since", [0.0, REPRESENT_SECONDS - 0.01])
    def test_the_limit_is_respected(self, since: float) -> None:
        assert not should_reassert(showing=True, active=False, since_last=since)


class TestWhenThereIsNoBreak:
    def test_nothing_is_presented(self) -> None:
        """Otherwise a finished break would keep grabbing the screen."""
        assert not should_reassert(showing=False, active=False, since_last=60.0)

    def test_not_even_a_focused_one(self) -> None:
        assert not should_reassert(showing=False, active=True, since_last=60.0)
