"""Putting the machine back, whatever state Anchor was in (SPEC 7.6, P4)."""

from __future__ import annotations

from pathlib import Path

import pytest

from anchor.blocker.journal import Journal
from anchor.blocker.restore import restore_everything
from anchor.system.commands import RecordingRunner, Result


@pytest.fixture
def journal(tmp_path: Path) -> Journal:
    return Journal(tmp_path / "applied.json")


@pytest.fixture
def runner() -> RecordingRunner:
    return RecordingRunner()


class TestTheTableAlwaysGoes:
    def test_the_nftables_table_is_deleted(self, journal: Journal, runner: RecordingRunner) -> None:
        journal.record_nft_table()
        report = restore_everything(journal, runner=runner)

        assert runner.ran("nft delete table inet anchor")
        assert report.removed_table

    def test_the_table_is_deleted_even_with_an_empty_journal(
        self, journal: Journal, runner: RecordingRunner
    ) -> None:
        """A lost journal must not leave rules behind (SPEC 7.6)."""
        restore_everything(journal, runner=runner)
        assert runner.ran("nft delete table inet anchor")

    def test_a_missing_table_is_not_an_error(self, journal: Journal) -> None:
        runner = RecordingRunner({"nft delete": Result(code=1, err="No such file or directory")})
        report = restore_everything(journal, runner=runner)

        assert not report.problems
        assert not report.removed_table

    def test_a_real_nft_failure_is_reported(self, journal: Journal) -> None:
        runner = RecordingRunner({"nft delete": Result(code=1, err="Operation not permitted")})
        report = restore_everything(journal, runner=runner)

        assert any("not permitted" in problem for problem in report.problems)


class TestLinks:
    def test_recorded_links_are_reverted(self, journal: Journal, runner: RecordingRunner) -> None:
        journal.record_link("eth0")
        journal.record_link("wlan0")

        report = restore_everything(journal, runner=runner)

        assert runner.ran("resolvectl revert eth0")
        assert runner.ran("resolvectl revert wlan0")
        assert report.reverted_links == ["eth0", "wlan0"]

    def test_links_are_forgotten_once_reverted(
        self, journal: Journal, runner: RecordingRunner
    ) -> None:
        journal.record_link("eth0")
        restore_everything(journal, runner=runner)

        assert journal.load().links == []

    def test_no_links_means_no_resolvectl(self, journal: Journal, runner: RecordingRunner) -> None:
        restore_everything(journal, runner=runner)
        assert not runner.ran("resolvectl revert")


class TestFiles:
    def test_recorded_files_are_put_back(
        self, journal: Journal, runner: RecordingRunner, tmp_path: Path
    ) -> None:
        policy = tmp_path / "policies.json"
        policy.write_text('{"policies": {"Original": true}}', encoding="utf-8")
        journal.write_file(policy, '{"policies": {"DNSOverHTTPS": false}}')

        report = restore_everything(journal, runner=runner)

        assert policy.read_text(encoding="utf-8") == '{"policies": {"Original": true}}'
        assert report.restored_files == 1


class TestItIsAlwaysSafeToRun:
    def test_restoring_a_clean_machine_does_nothing_bad(
        self, journal: Journal, runner: RecordingRunner
    ) -> None:
        report = restore_everything(journal, runner=runner)

        assert not report.problems
        assert report.restored_files == 0
        assert report.reverted_links == []

    def test_restoring_twice_is_harmless(
        self, journal: Journal, runner: RecordingRunner, tmp_path: Path
    ) -> None:
        journal.record_nft_table()
        journal.record_link("eth0")
        journal.write_file(tmp_path / "x.json", "{}")

        restore_everything(journal, runner=runner)
        second = restore_everything(journal, runner=runner)

        assert not second.problems
        assert journal.load().is_empty

    def test_a_corrupt_journal_still_clears_the_rules(
        self, journal: Journal, runner: RecordingRunner
    ) -> None:
        journal.path.parent.mkdir(parents=True, exist_ok=True)
        journal.path.write_text("}{ not json", encoding="utf-8")

        restore_everything(journal, runner=runner)

        assert runner.ran("nft delete table inet anchor")


class TestTheRefusalDropIns:
    def test_the_refuse_stop_drop_ins_are_removed(
        self, journal: Journal, runner: RecordingRunner, tmp_path: Path
    ) -> None:
        """Removal must be able to stop the services it is removing (SPEC 5.3)."""
        runtime = tmp_path / "run" / "systemd" / "system"
        drop_in = runtime / "anchord.service.d" / "anchor-session.conf"
        drop_in.parent.mkdir(parents=True)
        drop_in.write_text("[Unit]\nRefuseManualStop=yes\n", encoding="utf-8")

        restore_everything(journal, runner=runner, systemd_runtime_dir=runtime)

        assert not drop_in.exists()
        assert runner.ran("systemctl daemon-reload")

    def test_no_drop_ins_means_no_reload(
        self, journal: Journal, runner: RecordingRunner, tmp_path: Path
    ) -> None:
        restore_everything(journal, runner=runner, systemd_runtime_dir=tmp_path / "empty")
        assert not runner.ran("systemctl daemon-reload")
