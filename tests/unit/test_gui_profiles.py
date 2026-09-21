"""The Profiles screen (SPEC 12, 7.4, mockup 4)."""

from __future__ import annotations

from typing import Any

from anchor.gui.profiles import PATTERNS, edit_request, profile_view


def profile(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "Study",
        "web_mode": "blocklist",
        "categories": ["social", "video"],
        "domains": ["news.ycombinator.com", "twitch.tv"],
        "apps": ["discord.desktop"],
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


CATEGORIES = [
    {"id": "social", "name": "Social media", "domains": ["x.com"], "apps": ["discord.desktop"]},
    {"id": "video", "name": "Video", "domains": ["youtube.com"], "apps": []},
    {"id": "news", "name": "News", "domains": ["bbc.com"], "apps": []},
]

APPS = [
    {"id": "discord.desktop", "name": "Discord", "icon": "discord"},
    {"id": "steam.desktop", "name": "Steam", "icon": "steam"},
]


def view(**overrides: Any) -> Any:
    settings: dict[str, Any] = {
        "profile": profile(),
        "categories": CATEGORIES,
        "apps": APPS,
        "in_use": False,
    }
    settings.update(overrides)
    return profile_view(**settings)


class TestTheWebList:
    def test_the_mode_is_shown_and_can_be_changed(self) -> None:
        assert view().mode == "blocklist"
        assert not view().mode_locked

    def test_the_allowlist_warning_is_always_there_to_read(self) -> None:
        """SPEC 8.1 asks for a clear warning, and the mockup prints it."""
        assert "other domains" in view().allowlist_warning

    def test_an_allowlist_in_use_cannot_become_a_blocklist_again(self) -> None:
        locked = view(profile=profile(web_mode="allowlist"), in_use=True)
        assert locked.mode_locked
        assert "unblock the rest of the web" in locked.mode_reason

    def test_a_blocklist_in_use_can_still_become_an_allowlist(self) -> None:
        """That direction is stricter, and the ratchet allows stricter."""
        assert not view(in_use=True).mode_locked


class TestTheCategories:
    def test_every_installed_category_is_offered(self) -> None:
        assert [one.key for one in view().categories] == ["news", "social", "video"]

    def test_the_ones_in_the_profile_are_ticked(self) -> None:
        ticked = {one.key: one.on for one in view().categories}
        assert ticked == {"news": False, "social": True, "video": True}

    def test_each_says_what_it_covers(self) -> None:
        social = [one for one in view().categories if one.key == "social"][0]
        assert "1 site" in social.detail
        assert "1 app" in social.detail

    def test_during_a_session_a_ticked_one_cannot_be_unticked(self) -> None:
        during = {one.key: one.locked for one in view(in_use=True).categories}
        assert during == {"news": False, "social": True, "video": True}


class TestTheDomains:
    def test_the_profile_s_own_domains_are_listed(self) -> None:
        assert [one.value for one in view().domains] == [
            "news.ycombinator.com",
            "twitch.tv",
        ]

    def test_they_can_be_removed_outside_a_session(self) -> None:
        assert all(one.removable for one in view().domains)

    def test_a_blocklist_in_use_will_not_give_one_up(self) -> None:
        during = view(in_use=True)
        assert not any(one.removable for one in during.domains)
        assert "during a session" in during.domains[0].reason

    def test_an_allowlist_in_use_refuses_the_other_direction(self) -> None:
        """Adding to an allowlist opens a site, so it is adding that is barred."""
        during = view(profile=profile(web_mode="allowlist"), in_use=True)
        assert all(one.removable for one in during.domains)
        assert not during.can_add_domains
        assert "unblock" in during.add_reason


class TestTheApplications:
    def test_every_installed_application_is_offered_with_its_name(self) -> None:
        assert [one.name for one in view().apps] == ["Discord", "Steam"]

    def test_the_ones_in_the_profile_are_ticked(self) -> None:
        assert [one.on for one in view().apps] == [True, False]

    def test_an_app_a_category_already_blocks_says_so(self) -> None:
        """Because unticking it would change nothing, and looks broken."""
        discord = view().apps[0]
        assert "Social media" in discord.detail

    def test_during_a_session_a_ticked_one_cannot_be_unticked(self) -> None:
        assert [one.locked for one in view(in_use=True).apps] == [True, False]


class TestTheBreaks:
    def test_the_patterns_the_mockup_offers_are_offered(self) -> None:
        assert [one.key for one in view().patterns] == ["25/5", "50/10", "90/20", "custom"]

    def test_the_profile_s_own_pattern_is_the_chosen_one(self) -> None:
        assert view().pattern == "50/10"

    def test_a_pattern_of_its_own_is_called_custom(self) -> None:
        odd = view(profile=profile(breaks={**profile()["breaks"], "work_minutes": 37}))
        assert odd.pattern == "custom"

    def test_the_type_and_hardness_are_chosen_from_the_spec_s_lists(self) -> None:
        assert [one.key for one in view().types] == ["notification", "overlay"]
        assert [one.key for one in view().hardnesses] == ["flexible", "moderate", "mandatory"]

    def test_each_hardness_says_what_it_allows(self) -> None:
        flexible, moderate, mandatory = view().hardnesses
        assert "skip" in flexible.detail
        assert "once" in moderate.detail
        assert "neither" in mandatory.detail.lower()

    def test_allowing_sites_during_breaks_is_a_choice(self) -> None:
        assert not view().allow_sites.on
        assert not view().allow_sites.locked

    def test_but_not_one_that_can_be_turned_on_mid_session(self) -> None:
        during = view(in_use=True).allow_sites
        assert during.locked
        assert "session" in during.reason

    def test_and_can_still_be_turned_off_mid_session(self) -> None:
        """Turning it off is stricter, so the ratchet has no objection."""
        on = profile(breaks={**profile()["breaks"], "allow_sites_during_breaks": True})
        assert not view(profile=on, in_use=True).allow_sites.locked


class TestSaving:
    def test_nothing_changed_sends_nothing(self) -> None:
        assert edit_request(profile(), profile()) is None

    def test_an_added_domain_is_sent_as_an_addition(self) -> None:
        after = profile(domains=[*profile()["domains"], "x.com"])
        _type, payload = edit_request(profile(), after)  # type: ignore[misc]
        assert payload["add_domains"] == ["x.com"]
        assert "remove_domains" not in payload

    def test_a_removed_application_is_sent_as_a_removal(self) -> None:
        _type, payload = edit_request(profile(), profile(apps=[]))  # type: ignore[misc]
        assert payload["remove_apps"] == ["discord.desktop"]

    def test_the_mode_is_only_sent_when_it_changed(self) -> None:
        _type, payload = edit_request(profile(), profile(web_mode="allowlist"))  # type: ignore[misc]
        assert payload["web_mode"] == "allowlist"

    def test_break_changes_go_in_one_object(self) -> None:
        after = profile(breaks={**profile()["breaks"], "work_minutes": 25, "break_minutes": 5})
        _type, payload = edit_request(profile(), after)  # type: ignore[misc]
        assert payload["breaks"] == {"work_minutes": 25, "break_minutes": 5}

    def test_dropping_the_long_break_is_sent_as_a_zero(self) -> None:
        before = profile(
            breaks={**profile()["breaks"], "long_break_every": 4, "long_break_minutes": 20}
        )
        _type, payload = edit_request(before, profile())  # type: ignore[misc]
        assert payload["breaks"]["long_break_every"] == 0

    def test_the_name_always_goes_because_it_says_which_profile(self) -> None:
        _type, payload = edit_request(profile(), profile(domains=[]))  # type: ignore[misc]
        assert payload["name"] == "Study"


class TestThePatterns:
    def test_they_are_the_three_the_spec_names(self) -> None:
        assert PATTERNS == ((25, 5), (50, 10), (90, 20))
