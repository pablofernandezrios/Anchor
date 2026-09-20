"""Categories: names that stand for domains and applications (SPEC 9, 12)."""

from __future__ import annotations

from pathlib import Path

import pytest

from anchor.engine.categories import (
    Category,
    load_categories,
    parse_category,
    resolve,
)

DATA = Path(__file__).resolve().parents[2] / "data" / "categories"

SOCIAL = """\
name = "Social media"
domains = ["facebook.com", "discord.com"]
apps = ["discord.desktop"]
"""


def write(directory: Path, name: str, text: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


class TestReadingOne:
    def test_the_identifier_is_the_file_name(self, tmp_path: Path) -> None:
        """A profile stores the identifier, so it must not be the display name."""
        category = parse_category(write(tmp_path, "social.toml", SOCIAL))

        assert category is not None
        assert category.id == "social"
        assert category.name == "Social media"

    def test_domains_and_apps_come_through(self, tmp_path: Path) -> None:
        category = parse_category(write(tmp_path, "social.toml", SOCIAL))

        assert category is not None
        assert category.domains == frozenset({"facebook.com", "discord.com"})
        assert category.apps == frozenset({"discord.desktop"})

    def test_domains_are_lowercased(self, tmp_path: Path) -> None:
        """Names are case-insensitive, and the matcher compares them lowered."""
        category = parse_category(
            write(tmp_path, "social.toml", 'name = "S"\ndomains = ["FaceBook.COM"]')
        )

        assert category is not None
        assert category.domains == frozenset({"facebook.com"})

    def test_the_lists_are_optional(self, tmp_path: Path) -> None:
        category = parse_category(write(tmp_path, "empty.toml", 'name = "Empty"'))

        assert category is not None
        assert category.domains == frozenset()
        assert category.apps == frozenset()

    def test_blank_entries_are_dropped(self, tmp_path: Path) -> None:
        category = parse_category(
            write(tmp_path, "x.toml", 'name = "X"\ndomains = ["a.com", "", "  "]')
        )

        assert category is not None
        assert category.domains == frozenset({"a.com"})


class TestFilesThatAreWrong:
    def test_broken_toml_is_skipped_rather_than_fatal(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A stray character in a list must not stop the engine from starting."""
        with caplog.at_level("WARNING", logger="anchord"):
            assert parse_category(write(tmp_path, "bad.toml", 'name = "X\ndomains =')) is None

        assert "bad.toml" in caplog.text

    def test_a_category_with_no_name_is_skipped(self, tmp_path: Path) -> None:
        assert parse_category(write(tmp_path, "x.toml", 'domains = ["a.com"]')) is None

    def test_a_blank_name_is_no_name(self, tmp_path: Path) -> None:
        assert parse_category(write(tmp_path, "x.toml", 'name = "   "')) is None

    def test_a_list_that_is_not_a_list_is_ignored(self, tmp_path: Path) -> None:
        category = parse_category(write(tmp_path, "x.toml", 'name = "X"\ndomains = "facebook.com"'))

        assert category is not None
        assert category.domains == frozenset()

    def test_entries_that_are_not_text_are_ignored(self, tmp_path: Path) -> None:
        category = parse_category(
            write(tmp_path, "x.toml", 'name = "X"\ndomains = ["a.com", 7, true]')
        )

        assert category is not None
        assert category.domains == frozenset({"a.com"})

    def test_a_file_that_cannot_be_read_is_skipped(self, tmp_path: Path) -> None:
        assert parse_category(tmp_path / "missing.toml") is None


class TestLoadingADirectory:
    def test_every_file_is_read(self, tmp_path: Path) -> None:
        write(tmp_path, "social.toml", SOCIAL)
        write(tmp_path, "video.toml", 'name = "Video"\ndomains = ["youtube.com"]')

        assert sorted(load_categories(tmp_path)) == ["social", "video"]

    def test_a_missing_directory_is_not_an_error(self, tmp_path: Path) -> None:
        assert load_categories(tmp_path / "nowhere") == {}

    def test_one_bad_file_does_not_lose_the_others(self, tmp_path: Path) -> None:
        write(tmp_path, "social.toml", SOCIAL)
        write(tmp_path, "broken.toml", "name = ")

        assert sorted(load_categories(tmp_path)) == ["social"]

    def test_the_users_copy_replaces_the_shipped_one(self, tmp_path: Path) -> None:
        """SPEC 12 asks for lists that are editable AND updated with the package."""
        shipped = tmp_path / "usr"
        mine = tmp_path / "etc"
        write(shipped, "social.toml", SOCIAL)
        write(mine, "social.toml", 'name = "Mine"\ndomains = ["only-this.com"]')

        loaded = load_categories(shipped, mine)

        assert loaded["social"].name == "Mine"
        assert loaded["social"].domains == frozenset({"only-this.com"})

    def test_it_replaces_rather_than_merges(self, tmp_path: Path) -> None:
        """Otherwise a user could never take a domain out of a shipped list."""
        shipped = tmp_path / "usr"
        mine = tmp_path / "etc"
        write(shipped, "social.toml", SOCIAL)
        write(mine, "social.toml", 'name = "Mine"\ndomains = ["only-this.com"]')

        assert "facebook.com" not in load_categories(shipped, mine)["social"].domains

    def test_the_users_own_categories_are_added(self, tmp_path: Path) -> None:
        shipped = tmp_path / "usr"
        mine = tmp_path / "etc"
        write(shipped, "social.toml", SOCIAL)
        write(mine, "work.toml", 'name = "Work"\ndomains = ["jira.example"]')

        assert sorted(load_categories(shipped, mine)) == ["social", "work"]


class TestResolving:
    def known(self) -> dict[str, Category]:
        return {
            "social": Category(
                id="social",
                name="Social media",
                domains=frozenset({"discord.com"}),
                apps=frozenset({"discord.desktop"}),
            ),
            "video": Category(id="video", name="Video", domains=frozenset({"youtube.com"})),
        }

    def test_a_category_brings_its_domains_and_its_app(self) -> None:
        """SPEC 9's example: Discord means the program and discord.com."""
        resolved = resolve(frozenset({"social"}), self.known())

        assert resolved.domains == frozenset({"discord.com"})
        assert resolved.apps == frozenset({"discord.desktop"})

    def test_several_categories_are_added_together(self) -> None:
        resolved = resolve(frozenset({"social", "video"}), self.known())

        assert resolved.domains == frozenset({"discord.com", "youtube.com"})

    def test_a_category_that_is_not_installed_is_skipped(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A deleted list must not stop a session from starting."""
        with caplog.at_level("INFO", logger="anchord"):
            resolved = resolve(frozenset({"social", "gone"}), self.known())

        assert resolved.domains == frozenset({"discord.com"})
        assert "gone" in caplog.text

    def test_no_categories_bring_nothing(self) -> None:
        resolved = resolve(frozenset(), self.known())

        assert resolved.domains == frozenset()
        assert resolved.apps == frozenset()


class TestWhatIsShipped:
    """The five SPEC 12 asks for, checked as data rather than as code."""

    def test_all_five_are_present(self) -> None:
        assert sorted(load_categories(DATA)) == [
            "games",
            "news",
            "shopping",
            "social",
            "video",
        ]

    def test_they_are_the_names_the_specification_lists(self) -> None:
        names = sorted(category.name for category in load_categories(DATA).values())

        assert names == ["Games", "News", "Shopping", "Social media", "Video"]

    def test_none_of_them_is_empty(self) -> None:
        for category in load_categories(DATA).values():
            assert category.domains, f"{category.id} blocks nothing"

    def test_social_media_bundles_discord_with_its_site(self) -> None:
        """This is SPEC 9's worked example, so it is worth pinning down."""
        social = load_categories(DATA)["social"]

        assert "discord.com" in social.domains
        assert "discord.desktop" in social.apps

    def test_applications_are_named_by_their_desktop_entry(self) -> None:
        """Anything else would never match, because that is the identifier."""
        for category in load_categories(DATA).values():
            for app in category.apps:
                assert app.endswith(".desktop"), f"{category.id} names {app!r}"

    def test_no_domain_carries_a_scheme_or_a_path(self) -> None:
        for category in load_categories(DATA).values():
            for domain in category.domains:
                assert "/" not in domain, f"{category.id} names {domain!r}"
                assert domain == domain.strip().lower()


class TestADirectoryThatCannotBeListed:
    def test_it_is_skipped_rather_than_fatal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A bad mode on /etc/anchor/categories must not stop the engine."""

        def refuse(self: Path, pattern: str) -> object:
            raise PermissionError("not allowed to list this")

        monkeypatch.setattr(Path, "glob", refuse)

        assert load_categories(Path("/usr/share/anchor/categories")) == {}
