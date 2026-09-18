"""Only stricter changes are accepted during a session (SPEC 7.4, P3)."""

from __future__ import annotations

import pytest

from anchor.engine.profiles import Profile
from anchor.engine.ratchet import check_profile_change, check_session_change
from anchor.protocol.errors import ErrorCode, RatchetViolationError
from anchor.protocol.types import Level, Valve, WebMode


def profile(**overrides: object) -> Profile:
    base: dict[str, object] = {
        "name": "Study",
        "web_mode": WebMode.BLOCKLIST,
        "categories": frozenset({"social", "video"}),
        "domains": frozenset({"twitch.tv"}),
        "apps": frozenset({"discord"}),
    }
    base.update(overrides)
    return Profile(**base)  # type: ignore[arg-type]


class TestAccepted:
    def test_adding_a_domain_is_allowed(self) -> None:
        check_profile_change(profile(), profile(domains=frozenset({"twitch.tv", "x.com"})))

    def test_adding_an_app_is_allowed(self) -> None:
        check_profile_change(profile(), profile(apps=frozenset({"discord", "steam"})))

    def test_adding_a_category_is_allowed(self) -> None:
        check_profile_change(profile(), profile(categories=frozenset({"social", "video", "news"})))

    def test_changing_nothing_is_allowed(self) -> None:
        check_profile_change(profile(), profile())

    def test_switching_to_an_allowlist_is_stricter(self) -> None:
        """An allowlist blocks everything except what it names (SPEC 8.1)."""
        check_profile_change(profile(), profile(web_mode=WebMode.ALLOWLIST))

    def test_removing_an_allowlist_entry_is_stricter(self) -> None:
        before = profile(web_mode=WebMode.ALLOWLIST, domains=frozenset({"a.com", "b.com"}))
        after = profile(web_mode=WebMode.ALLOWLIST, domains=frozenset({"a.com"}))
        check_profile_change(before, after)


class TestRejected:
    def test_removing_a_blocked_domain_is_refused(self) -> None:
        with pytest.raises(RatchetViolationError) as caught:
            check_profile_change(profile(), profile(domains=frozenset()))
        assert caught.value.code is ErrorCode.RATCHET_VIOLATION

    def test_removing_a_blocked_app_is_refused(self) -> None:
        with pytest.raises(RatchetViolationError):
            check_profile_change(profile(), profile(apps=frozenset()))

    def test_removing_a_category_is_refused(self) -> None:
        with pytest.raises(RatchetViolationError):
            check_profile_change(profile(), profile(categories=frozenset({"social"})))

    def test_switching_an_allowlist_back_to_a_blocklist_is_refused(self) -> None:
        before = profile(web_mode=WebMode.ALLOWLIST)
        with pytest.raises(RatchetViolationError):
            check_profile_change(before, profile(web_mode=WebMode.BLOCKLIST))

    def test_adding_to_an_allowlist_is_refused(self) -> None:
        """Adding to an allowlist unblocks a site, so it loosens the session."""
        before = profile(web_mode=WebMode.ALLOWLIST, domains=frozenset({"a.com"}))
        after = profile(web_mode=WebMode.ALLOWLIST, domains=frozenset({"a.com", "b.com"}))
        with pytest.raises(RatchetViolationError):
            check_profile_change(before, after)

    def test_the_error_names_what_was_refused(self) -> None:
        with pytest.raises(RatchetViolationError, match="twitch.tv"):
            check_profile_change(profile(), profile(domains=frozenset()))


class TestSessionChanges:
    def test_raising_the_level_is_allowed(self) -> None:
        check_session_change(Level.SOFT, Level.FIRM, valve_before=None, valve_after=None)

    def test_lowering_the_level_is_refused(self) -> None:
        with pytest.raises(RatchetViolationError):
            check_session_change(
                Level.STRICT, Level.FIRM, valve_before=Valve.WAIT, valve_after=None
            )

    def test_keeping_the_level_is_allowed(self) -> None:
        check_session_change(Level.FIRM, Level.FIRM, valve_before=None, valve_after=None)

    def test_changing_the_valve_is_refused(self) -> None:
        """SPEC 7.5: the valve is chosen at start and cannot change."""
        with pytest.raises(RatchetViolationError, match="valve"):
            check_session_change(
                Level.STRICT, Level.STRICT, valve_before=Valve.WAIT, valve_after=Valve.PHRASE
            )

    def test_keeping_the_valve_is_allowed(self) -> None:
        check_session_change(
            Level.STRICT, Level.STRICT, valve_before=Valve.BOTH, valve_after=Valve.BOTH
        )
