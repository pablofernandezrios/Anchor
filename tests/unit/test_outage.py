"""What a screen says when it has no answer to draw (SPEC 14).

The first run of the interface on a real desktop found this: with the engine
stopped, Home degraded correctly and the other five pages were blank
rectangles under a toast that vanished after six seconds.
"""

from __future__ import annotations

from anchor.gui.outage import UNREACHABLE, outage


class TestTheEngineBeingDown:
    def test_it_says_so_in_words_a_person_can_read(self) -> None:
        view = outage(UNREACHABLE)

        assert "not running" in view.title
        assert view.detail

    def test_and_says_how_to_fix_it(self) -> None:
        """The one failure the user can act on themselves."""
        assert "systemctl start anchord" in outage(UNREACHABLE).remedy


class TestAnyOtherRefusal:
    def test_the_engines_own_words_are_what_is_shown(self) -> None:
        """Anchor explaining the engine to the user would say less, not more."""
        view = outage("INVALID_CONFIG", "there is no profile called 'Work'")

        assert view.detail == "there is no profile called 'Work'"

    def test_a_failure_with_nothing_to_say_still_says_something(self) -> None:
        view = outage("INVALID_CONFIG", "")

        assert view.title
        assert view.detail

    def test_only_the_engine_being_down_offers_a_remedy(self) -> None:
        """Inventing one for a refusal would be guessing at the cause."""
        assert outage("RATCHET_VIOLATION", "no").remedy == ""
