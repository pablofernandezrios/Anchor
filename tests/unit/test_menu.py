"""The indicator's menu as dbusmenu describes it (SPEC 14.1, ADR 3)."""

from __future__ import annotations

from anchor.agent.indicator import IndicatorModel, MenuItem
from anchor.agent.menu import (
    ROOT_ID,
    action_at,
    group_properties,
    layout_for,
    properties_of,
    with_separator,
)

MENU = (
    MenuItem("Study · Firm", enabled=False),
    MenuItem("2:14:37 left · ends at 13:30", enabled=False),
    MenuItem("Next break in 18 min", enabled=False),
    MenuItem("Blocked attempts: 7", enabled=False),
    MenuItem("Extend session", action="extend"),
    MenuItem("Open Anchor", action="open"),
)


class TestTheSeparator:
    def test_it_goes_between_what_is_read_and_what_is_clicked(self) -> None:
        kinds = [one.action == "separator" for one in with_separator(MENU)]
        assert kinds == [False, False, False, False, True, False, False]

    def test_a_menu_that_is_all_actions_gets_none(self) -> None:
        only = (MenuItem("Open Anchor", action="open"),)
        assert with_separator(only) == only

    def test_a_menu_that_is_all_words_gets_none(self) -> None:
        said = (MenuItem("Anchor cannot reach its engine", enabled=False),)
        assert with_separator(said) == said

    def test_only_one_is_added(self) -> None:
        many = MENU + (MenuItem("Third", action="third"),)
        assert sum(one.action == "separator" for one in with_separator(many)) == 1


class TestProperties:
    def test_a_line_that_does_something_is_enabled(self) -> None:
        assert properties_of(MenuItem("Extend session", action="extend"))["enabled"] is True

    def test_a_line_that_is_only_information_is_not(self) -> None:
        """Clickable text that does nothing is a small lie told all day."""
        assert properties_of(MenuItem("Blocked attempts: 7", enabled=False))["enabled"] is False

    def test_a_separator_says_it_is_one(self) -> None:
        assert properties_of(MenuItem("", action="separator"))["type"] == "separator"

    def test_an_underscore_survives_the_menu(self) -> None:
        """Otherwise a profile called deep_work loses a letter to a mnemonic."""
        assert properties_of(MenuItem("deep_work · Firm"))["label"] == "deep__work · Firm"


class TestTheLayout:
    def test_the_root_is_zero_and_holds_everything(self) -> None:
        root, properties, children = layout_for(MENU)

        assert root == ROOT_ID
        assert properties["children-display"] == "submenu"
        assert len(children) == len(MENU) + 1  # the separator

    def test_children_are_numbered_from_one(self) -> None:
        _root, _properties, children = layout_for(MENU)
        assert [child[0] for child in children] == list(range(1, len(children) + 1))

    def test_each_child_carries_its_own_properties(self) -> None:
        _root, _properties, children = layout_for(MENU)
        assert children[0][1]["label"] == "Study · Firm"

    def test_nothing_has_children_of_its_own(self) -> None:
        """One flat menu. SPEC 14.1 lists six lines, not a tree."""
        _root, _properties, children = layout_for(MENU)
        assert all(child[2] == [] for child in children)


class TestAskingAboutSomeOfThem:
    def test_only_what_was_asked_for_comes_back(self) -> None:
        assert [one[0] for one in group_properties(MENU, [1, 3])] == [1, 3]

    def test_asking_for_nothing_means_asking_for_everything(self) -> None:
        assert len(group_properties(MENU, [])) == len(MENU) + 1

    def test_an_identifier_nobody_has_is_ignored(self) -> None:
        assert [one[0] for one in group_properties(MENU, [1, 99])] == [1]


class TestClicking:
    def test_a_line_that_does_something_says_what(self) -> None:
        assert action_at(MENU, 6) == "extend"

    def test_a_line_that_is_only_information_does_nothing(self) -> None:
        assert action_at(MENU, 1) == ""

    def test_the_separator_does_nothing(self) -> None:
        assert action_at(MENU, 5) == ""

    def test_an_identifier_nobody_has_does_nothing(self) -> None:
        assert action_at(MENU, 99) == ""

    def test_a_disabled_action_does_nothing_either(self) -> None:
        held = (MenuItem("Extend session", action="extend", enabled=False),)
        assert action_at(held, 1) == ""


class TestAgainstTheRealIndicator:
    """The menu the model actually produces, not one written for the test."""

    def test_a_running_session_gives_a_menu_that_can_be_exported(self) -> None:
        model = IndicatorModel()
        model.update_status(
            {
                "active": True,
                "profile": "Study",
                "level": "firm",
                "remaining_seconds": 8077.0,
                "ends_at": 1_760_014_000.0,
                "blocked_attempts": 7,
                "phase": "working",
                "break": {"phase": "working", "remaining_seconds": 1080.0},
            }
        )

        _root, _properties, children = layout_for(model.view.menu)
        labels = [child[1].get("label", "") for child in children]
        assert "Extend session" in labels
        assert "Open Anchor" in labels

    def test_every_action_in_it_is_reachable_by_a_click(self) -> None:
        model = IndicatorModel()
        model.update_status({"active": True, "profile": "Study", "level": "soft"})
        menu = model.view.menu

        reachable = {action_at(menu, index) for index in range(1, len(with_separator(menu)) + 1)}
        assert {item.action for item in menu if item.action} <= reachable
