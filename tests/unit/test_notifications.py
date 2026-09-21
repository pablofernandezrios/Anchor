"""What Anchor says, and what it stays quiet about (SPEC 7.1, 8.3, 9)."""

from __future__ import annotations

from typing import Any

import pytest

from anchor.agent.notifications import (
    APPS_CHANNEL,
    BLOCKED_CHANNEL,
    Urgency,
    plan,
)


def shown(event: str, payload: dict[str, Any]) -> Any:
    notification = plan(event, payload)
    assert notification is not None, f"{event} said nothing"
    return notification


class TestABlockedSite:
    def test_the_domain_is_named(self) -> None:
        note = shown("blocked.attempt", {"domain": "youtube.com", "rule": "youtube.com"})

        assert note.summary == "Site blocked"
        assert "youtube.com" in note.body

    def test_a_subdomain_says_which_rule_caught_it(self) -> None:
        """The user blocked youtube.com and is looking at m.youtube.com."""
        note = shown("blocked.attempt", {"domain": "m.youtube.com", "rule": "youtube.com"})

        assert "m.youtube.com" in note.body
        assert "by the rule youtube.com" in note.body

    def test_they_replace_each_other_rather_than_stacking(self) -> None:
        """Fifteen refusals in a row are one thing that happened."""
        assert shown("blocked.attempt", {"domain": "a.com", "rule": "a.com"}).channel == (
            BLOCKED_CHANNEL
        )

    def test_it_is_not_shouted(self) -> None:
        note = shown("blocked.attempt", {"domain": "a.com", "rule": "a.com"})
        assert note.urgency is Urgency.NORMAL

    def test_an_event_with_no_domain_says_nothing(self) -> None:
        assert plan("blocked.attempt", {"domain": "", "rule": ""}) is None


class TestTheGraceWarning:
    def payload(self, **overrides: Any) -> dict[str, Any]:
        base: dict[str, Any] = {"apps": ["Discord"], "seconds": 120}
        base.update(overrides)
        return base

    def test_it_says_what_will_close_and_when(self) -> None:
        note = shown("apps.grace", self.payload())

        assert note.summary == "This application will close in 2 minutes"
        assert "Discord" in note.body

    def test_it_tells_the_user_to_save(self) -> None:
        """This is the whole purpose of the two minutes (SPEC 7.1)."""
        assert "Save your work" in shown("apps.grace", self.payload()).body

    def test_several_applications_read_as_a_list(self) -> None:
        note = shown("apps.grace", self.payload(apps=["Discord", "Slack", "Steam"]))

        assert note.summary.startswith("These applications")
        assert "Discord, Slack and Steam" in note.body

    def test_two_applications_are_joined_with_and(self) -> None:
        note = shown("apps.grace", self.payload(apps=["Discord", "Slack"]))
        assert "Discord and Slack" in note.body

    def test_it_will_not_slide_past_unseen(self) -> None:
        """Unsaved work is the one thing a missed notification costs."""
        assert shown("apps.grace", self.payload()).urgency is Urgency.CRITICAL

    def test_it_disappears_when_the_applications_do(self) -> None:
        """A warning about something that already happened is worse than none."""
        assert shown("apps.grace", self.payload(seconds=120)).timeout_ms == 120_000

    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (120, "in 2 minutes"),
            (60, "in a minute"),
            (90, "in 2 minutes"),
            (30, "in 30 seconds"),
            (0, "now"),
        ],
    )
    def test_the_time_reads_the_way_a_person_says_it(self, seconds: int, expected: str) -> None:
        assert expected in shown("apps.grace", self.payload(seconds=seconds)).summary

    def test_an_empty_list_says_nothing(self) -> None:
        assert plan("apps.grace", {"apps": [], "seconds": 120}) is None


class TestAClosedApplication:
    def test_one_that_was_already_open(self) -> None:
        note = shown("apps.closed", {"apps": ["Discord"], "reason": "closed"})

        assert note.summary == "Anchor closed Discord"
        assert note.body == "It is blocked during this session."

    def test_several_at_once(self) -> None:
        note = shown("apps.closed", {"apps": ["Discord", "Steam"], "reason": "closed"})

        assert note.summary == "Anchor closed Discord and Steam"
        assert note.body.startswith("They are")

    def test_one_that_was_just_launched_is_worded_differently(self) -> None:
        """SPEC 9: it never got a window, and the user needs to know why."""
        note = shown("apps.closed", {"apps": ["Discord"], "reason": "launch"})

        assert note.summary == "Discord is blocked"
        assert "as it opened" in note.body

    def test_several_launched_at_once(self) -> None:
        note = shown("apps.closed", {"apps": ["Discord", "Steam"], "reason": "launch"})

        assert note.summary == "Discord and Steam are blocked"

    def test_they_share_a_channel_with_the_grace_warning(self) -> None:
        """The warning is about these applications; this replaces it."""
        assert shown("apps.closed", {"apps": ["Discord"]}).channel == APPS_CHANNEL

    def test_an_empty_list_says_nothing(self) -> None:
        assert plan("apps.closed", {"apps": [], "reason": "closed"}) is None

    def test_rubbish_in_the_list_is_ignored(self) -> None:
        assert plan("apps.closed", {"apps": ["  ", ""], "reason": "closed"}) is None


class TestSilence:
    """A focus tool that chats is a focus tool people turn off."""

    @pytest.mark.parametrize(
        "event",
        [
            "session.started",
            "session.tick",
            "session.ended",
            "session.extended",
            "valve.requested",
            "valve.withdrawn",
            "rupture.recorded",
            "profile.changed",
            "config.changed",
        ],
    )
    def test_nothing_else_produces_a_notification(self, event: str) -> None:
        assert plan(event, {"anything": True}) is None

    def test_an_unknown_event_is_not_an_error(self) -> None:
        assert plan("something.new", {}) is None

    def test_a_payload_of_the_wrong_shape_is_not_an_error(self) -> None:
        assert plan("apps.closed", {"apps": "Discord"}) is None
