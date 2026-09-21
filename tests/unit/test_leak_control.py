"""The leak test's control row can tell Anchor from the network (SPEC 8.4).

The five leak checks are run on a real machine in CI. This is about the sixth
row, the control, which is the one that catches Anchor blocking too much — and
which failed once on a runner for a reason that had nothing to do with Anchor.

A check that cannot tell "Anchor is wrong" from "the network is down" reports
one of them confidently and is wrong half the time. These tests are what makes
that row honest.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _leak_test() -> Any:
    """Load the script, which lives outside the package."""
    if "leak_test" in sys.modules:
        return sys.modules["leak_test"]
    spec = importlib.util.spec_from_file_location(
        "leak_test", ROOT / "tests" / "system" / "leak_test.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["leak_test"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def leak() -> Any:
    return _leak_test()


def answering(leak: Any, monkeypatch: pytest.MonkeyPatch, names: set[str]) -> None:
    monkeypatch.setattr(leak, "resolves", lambda name, attempts=3: name in names)


class TestTheControlRow:
    def test_an_unblocked_site_that_works_is_a_pass(
        self, leak: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        answering(leak, monkeypatch, {leak.ALLOWED})
        report = leak.Report()

        leak.check_an_unblocked_site(report)

        assert report.checks[-1].leaked is False
        assert report.checks[-1].unanswered is False

    def test_one_name_failing_while_others_work_is_a_leak(
        self, leak: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Name resolution works; this one name does not. That is Anchor."""
        answering(leak, monkeypatch, set(leak.OTHER_ALLOWED))
        report = leak.Report()

        leak.check_an_unblocked_site(report)

        assert report.checks[-1].leaked is True
        assert "did NOT resolve" in report.checks[-1].detail

    def test_nothing_resolving_at_all_is_not_judged_yet(
        self, leak: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """This is the case that failed a run once, and it is not an answer."""
        answering(leak, monkeypatch, set())
        report = leak.Report()

        leak.check_an_unblocked_site(report)

        assert report.checks[-1].unanswered is True
        assert report.checks[-1].leaked is False

    def test_a_lost_query_is_retried_rather_than_believed(
        self, leak: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One lost UDP packet on a busy runner is not evidence."""
        attempts: list[int] = []

        class Result:
            def __init__(self, text: str) -> None:
                self.stdout = text

        def flaky(*args: str, timeout: float = 20.0) -> Result:
            attempts.append(1)
            return Result("" if len(attempts) < 3 else "140.82.121.4 github.com")

        monkeypatch.setattr(leak, "run", flaky)
        monkeypatch.setattr(leak.time, "sleep", lambda seconds: None)

        assert leak.resolves(leak.ALLOWED) is True
        assert len(attempts) == 3


class TestTheVerdictLine:
    def test_an_unanswered_row_reads_as_neither(self, leak: Any) -> None:
        check = leak.Check("x", leaked=False, detail="d", unanswered=True)

        assert "????" in check.line()
        assert "LEAK" not in check.line()

    def test_a_leak_still_reads_as_a_leak(self, leak: Any) -> None:
        assert "LEAK" in leak.Check("x", leaked=True, detail="d").line()

    def test_an_unanswered_row_does_not_fail_the_run(self, leak: Any) -> None:
        """Nothing was proved, so nothing is claimed. It is not a failure."""
        report = leak.Report()
        report.add("x", False, "d", unanswered=True)

        assert report.leaked is False
