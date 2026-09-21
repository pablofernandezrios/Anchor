"""How the command line behaves at the edges (SPEC 15)."""

from __future__ import annotations

import subprocess
import sys

import pytest

from anchor.cli.durations import format_countdown, format_duration


class TestPipingToHead:
    """`anchor status | head` is an ordinary thing to type."""

    def test_a_closed_pipe_does_not_produce_a_traceback(self) -> None:
        # --help writes plenty and needs no engine, so it exercises the same
        # path without anything else having to be running.
        head = subprocess.run(
            f"{sys.executable} -m anchor.cli.main --help | head -2",
            shell=True,  # noqa: S602
            capture_output=True,
            text=True,
            check=False,
        )

        assert "BrokenPipeError" not in head.stderr
        assert "Traceback" not in head.stderr


class TestFormatting:
    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [(8077, "2:14:37"), (0, "0:00:00"), (59, "0:00:59"), (3600, "1:00:00")],
    )
    def test_the_countdown_matches_the_mockups(self, seconds: int, expected: str) -> None:
        assert format_countdown(seconds) == expected

    def test_a_negative_countdown_reads_as_zero(self) -> None:
        """A session that just expired must not show a negative clock."""
        assert format_countdown(-5) == "0:00:00"

    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [(9000, "2 h 30 min"), (7200, "2 h"), (600, "10 min"), (45, "45 s")],
    )
    def test_durations_read_the_way_the_interface_writes_them(
        self, seconds: int, expected: str
    ) -> None:
        assert format_duration(seconds) == expected


class TestRenderingCategories:
    """`anchor category list|show` (SPEC 15)."""

    def render(self, action: str, name: str | None, result: dict[str, object]) -> tuple[int, str]:
        import argparse
        import contextlib
        import io

        from anchor.cli.main import _render

        args = argparse.Namespace(command="category", action=action, name=name)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = _render(args, result)
        return code, out.getvalue()

    @property
    def listing(self) -> dict[str, object]:
        return {
            "categories": [
                {
                    "id": "social",
                    "name": "Social media",
                    "domains": ["discord.com", "facebook.com"],
                    "apps": ["discord.desktop"],
                },
                {"id": "news", "name": "News", "domains": ["bbc.com"], "apps": []},
            ]
        }

    def test_the_list_shows_what_each_one_covers(self) -> None:
        code, text = self.render("list", None, self.listing)

        assert code == 0
        assert "social" in text
        assert "Social media" in text
        assert "2 domains, 1 apps" in text

    def test_an_empty_list_says_so_rather_than_printing_nothing(self) -> None:
        code, text = self.render("list", None, {"categories": []})

        assert code == 0
        assert "No categories" in text

    def test_showing_one_prints_its_domains_and_apps(self) -> None:
        code, text = self.render("show", "social", self.listing)

        assert code == 0
        assert "discord.com, facebook.com" in text
        assert "discord.desktop" in text

    def test_a_category_with_no_apps_prints_a_dash(self) -> None:
        _code, text = self.render("show", "news", self.listing)

        assert "Apps: -" in text

    def test_an_unknown_name_is_an_error_not_a_silence(self) -> None:
        """A script that pipes this needs to hear about it in the exit code."""
        code, text = self.render("show", "nope", self.listing)

        assert code == 1
        assert text == ""


class TestOptionsThatWorkAnywhere:
    """SPEC 15: `--json` on every command, wherever a person puts it.

    Both positions matter. People type `anchor stats --json` far more often
    than `anchor --json stats`, and argparse answers the first one with a
    usage error unless every subcommand is given the option too.
    """

    def parsed(self, *argv: str) -> object:
        from anchor.cli.main import parse_args

        _parser, args = parse_args(list(argv))
        return args

    def test_json_before_the_command(self) -> None:
        assert self.parsed("--json", "status").json is True  # type: ignore[attr-defined]

    def test_json_after_the_command(self) -> None:
        assert self.parsed("status", "--json").json is True  # type: ignore[attr-defined]

    def test_json_after_a_nested_command(self) -> None:
        assert self.parsed("profile", "list", "--json").json is True  # type: ignore[attr-defined]

    def test_it_is_off_when_nobody_asked(self) -> None:
        assert self.parsed("status").json is False  # type: ignore[attr-defined]

    def test_a_subcommand_does_not_overwrite_the_global(self) -> None:
        """The bug this guards: a shared action's default clobbered it.

        `set_defaults` writes the default onto the action object, and
        `parents=` shares one action between the top level and every
        subcommand, so `anchor --json stats` quietly printed human output.
        """
        assert self.parsed("--json", "stats", "--day").json is True  # type: ignore[attr-defined]

    def test_root_works_in_both_places_too(self) -> None:
        assert self.parsed("--root", "/tmp/a", "status").root == "/tmp/a"  # type: ignore[attr-defined]
        assert self.parsed("status", "--root", "/tmp/b").root == "/tmp/b"  # type: ignore[attr-defined]

    def test_and_is_none_when_nobody_asked(self) -> None:
        assert self.parsed("status").root is None  # type: ignore[attr-defined]


class TestConfigCommand:
    """`anchor config get|set` (SPEC 15)."""

    def request(self, *argv: str) -> tuple[str, dict[str, object]]:
        from anchor.cli.main import _request_for, parse_args

        _parser, args = parse_args(list(argv))
        return _request_for(args)

    def render(self, result: dict[str, object]) -> str:
        import argparse
        import contextlib
        import io

        from anchor.cli.main import _render

        out = io.StringIO()
        args = argparse.Namespace(command="config", action="get")
        with contextlib.redirect_stdout(out):
            _render(args, result)
        return out.getvalue()

    @property
    def settings(self) -> dict[str, object]:
        return {
            "settings": [
                {
                    "key": "firm_wait_seconds",
                    "value": 900,
                    "is_default": True,
                    "explain": "How long a Firm session makes you wait.",
                    "during_session": False,
                },
                {
                    "key": "language",
                    "value": "es",
                    "is_default": False,
                    "explain": "The interface language.",
                    "during_session": True,
                },
            ]
        }

    def test_get_without_a_key_asks_for_everything(self) -> None:
        assert self.request("config", "get") == ("config.get", {})

    def test_get_with_a_key_asks_for_one(self) -> None:
        assert self.request("config", "get", "language") == ("config.get", {"key": "language"})

    def test_set_carries_the_value_as_text(self) -> None:
        # The engine owns what each setting may hold, so the command line
        # sends the characters the user typed and lets it judge them.
        assert self.request("config", "set", "phrase_length", "200") == (
            "config.set",
            {"key": "phrase_length", "value": "200"},
        )

    def test_the_listing_shows_the_value_and_whether_it_is_the_default(self) -> None:
        text = self.render(self.settings)

        assert "firm_wait_seconds" in text
        assert "900" in text
        assert "(default)" in text
        assert "es" in text

    def test_a_setting_a_session_freezes_says_so(self) -> None:
        assert "Not during a session." in self.render(self.settings)

    def test_one_setting_prints_its_value_alone(self) -> None:
        text = self.render({"settings": [self.settings["settings"][1]]})  # type: ignore[index]

        assert text.strip().splitlines()[0].startswith("language")

    def test_a_change_is_confirmed(self) -> None:
        import argparse
        import contextlib
        import io

        from anchor.cli.main import _render

        out = io.StringIO()
        args = argparse.Namespace(command="config", action="set")
        with contextlib.redirect_stdout(out):
            _render(args, {"key": "language", "value": "es"})

        assert "language is now es" in out.getvalue()
