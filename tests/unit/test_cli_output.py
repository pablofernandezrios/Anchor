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
