"""The Lists screen: the categories and what is in them (SPEC 12, 15)."""

from __future__ import annotations

from typing import Any

from anchor.gui.lists import edit_request, lists_view

CATEGORIES: list[dict[str, Any]] = [
    {
        "id": "social",
        "name": "Social media",
        "domains": ["x.com", "facebook.com"],
        "apps": ["discord.desktop"],
        "custom": False,
    },
    {
        "id": "news",
        "name": "News",
        "domains": ["bbc.com"],
        "apps": [],
        "custom": True,
    },
]


def card(name: str, **overrides: Any) -> Any:
    view = lists_view(CATEGORIES, **overrides)
    return [one for one in view.categories if one.name == name][0]


class TestWhatIsShown:
    def test_every_category_is_there_in_name_order(self) -> None:
        assert [one.name for one in lists_view(CATEGORIES).categories] == [
            "News",
            "Social media",
        ]

    def test_each_says_how_much_it_covers(self) -> None:
        assert card("Social media").summary == "2 sites · 1 app"

    def test_one_of_each_is_singular(self) -> None:
        assert card("News").summary == "1 site"

    def test_the_entries_are_listed_in_order(self) -> None:
        assert [one.value for one in card("Social media").domains] == [
            "facebook.com",
            "x.com",
        ]

    def test_a_shipped_list_says_it_will_keep_being_updated(self) -> None:
        assert "updated with it" in card("Social media").origin

    def test_your_own_copy_says_it_will_not_be(self) -> None:
        """Which is the whole point of the two directories (SPEC 12)."""
        assert card("News").custom
        assert "will not change it" in card("News").origin

    def test_an_empty_machine_says_something_rather_than_nothing(self) -> None:
        assert lists_view([]).empty


class TestWhileASessionIsRunning:
    def test_a_category_the_session_blocks_is_marked(self) -> None:
        assert card("Social media", blocked_now={"social"}).blocked_now

    def test_and_nothing_can_be_taken_out_of_it(self) -> None:
        held = card("Social media", blocked_now={"social"})
        assert not held.can_remove
        assert not any(one.removable for one in held.domains)
        assert "session" in held.domains[0].reason

    def test_the_others_are_untouched(self) -> None:
        free = card("News", blocked_now={"social"})
        assert free.can_remove
        assert all(one.removable for one in free.domains)


class TestEditing:
    def test_an_addition_becomes_a_request(self) -> None:
        assert edit_request("social", add_domains=["reddit.com"]) == (
            "category.edit",
            {"id": "social", "add_domains": ["reddit.com"]},
        )

    def test_duplicates_are_sent_once(self) -> None:
        _type, payload = edit_request("social", add_domains=["x.com", "x.com"])  # type: ignore[misc]
        assert payload["add_domains"] == ["x.com"]

    def test_a_rename_is_a_change_like_any_other(self) -> None:
        _type, payload = edit_request("social", rename="Socials")  # type: ignore[misc]
        assert payload["name"] == "Socials"

    def test_asking_for_nothing_sends_nothing(self) -> None:
        assert edit_request("social") is None
