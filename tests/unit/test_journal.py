"""Restoring the system exactly as Anchor found it (SPEC 7.6, P4).

Uninstalling is always allowed, so nothing here may depend on a session, on
the engine, or on the HMAC key being readable.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from anchor.blocker.journal import Journal


@pytest.fixture
def journal(tmp_path: Path) -> Journal:
    return Journal(tmp_path / "applied.json")


class TestRecordingFiles:
    def test_a_new_file_is_removed_on_restore(self, journal: Journal, tmp_path: Path) -> None:
        target = tmp_path / "etc" / "policies.json"

        journal.write_file(target, '{"policies": {}}')
        assert target.exists()

        journal.restore()
        assert not target.exists()

    def test_an_existing_file_gets_its_contents_back(
        self, journal: Journal, tmp_path: Path
    ) -> None:
        target = tmp_path / "resolved.conf"
        target.write_text("[Resolve]\nDNS=1.1.1.1\n", encoding="utf-8")

        journal.write_file(target, "[Resolve]\nDNS=127.0.0.1:5391\n")
        assert "5391" in target.read_text(encoding="utf-8")

        journal.restore()
        assert target.read_text(encoding="utf-8") == "[Resolve]\nDNS=1.1.1.1\n"

    def test_a_directory_anchor_created_is_cleaned_up(
        self, journal: Journal, tmp_path: Path
    ) -> None:
        target = tmp_path / "made" / "up" / "tree" / "anchor.json"

        journal.write_file(target, "{}")
        journal.restore()

        assert not target.exists()
        assert not (tmp_path / "made").exists()

    def test_a_directory_with_other_files_is_left_alone(
        self, journal: Journal, tmp_path: Path
    ) -> None:
        shared = tmp_path / "managed"
        shared.mkdir()
        neighbour = shared / "someone-elses.json"
        neighbour.write_text("{}", encoding="utf-8")

        journal.write_file(shared / "anchor.json", "{}")
        journal.restore()

        assert not (shared / "anchor.json").exists()
        assert neighbour.exists(), "another program's policy was deleted"

    def test_restoring_twice_is_harmless(self, journal: Journal, tmp_path: Path) -> None:
        target = tmp_path / "policies.json"
        journal.write_file(target, "{}")

        journal.restore()
        journal.restore()

        assert not target.exists()

    def test_restoring_nothing_is_harmless(self, journal: Journal) -> None:
        journal.restore()

    def test_a_file_deleted_behind_our_back_does_not_stop_the_restore(
        self, journal: Journal, tmp_path: Path
    ) -> None:
        first = tmp_path / "one.json"
        second = tmp_path / "two.json"
        journal.write_file(first, "{}")
        journal.write_file(second, "{}")

        first.unlink()
        journal.restore()

        assert not second.exists(), "the second file was skipped after the first went missing"


class TestSurvivingRestarts:
    def test_the_journal_is_readable_by_a_fresh_process(self, tmp_path: Path) -> None:
        path = tmp_path / "applied.json"
        target = tmp_path / "policies.json"

        Journal(path).write_file(target, "{}")

        # A new Journal, as a package removal script would create.
        Journal(path).restore()
        assert not target.exists()

    def test_the_journal_is_plain_json(self, journal: Journal, tmp_path: Path) -> None:
        """Restoration must not depend on the HMAC key (SPEC 7.6)."""
        journal.write_file(tmp_path / "x.json", "{}")

        document = json.loads(journal.path.read_text(encoding="utf-8"))
        assert "hmac" not in document
        assert document["files"][0]["path"] == str(tmp_path / "x.json")

    def test_a_corrupt_journal_does_not_block_restoration(self, journal: Journal) -> None:
        journal.path.parent.mkdir(parents=True, exist_ok=True)
        journal.path.write_text("this is not json", encoding="utf-8")

        journal.restore()  # must not raise: uninstalling is always allowed

    def test_the_journal_is_emptied_once_everything_is_back(
        self, journal: Journal, tmp_path: Path
    ) -> None:
        journal.write_file(tmp_path / "x.json", "{}")
        journal.restore()

        assert journal.load().files == []


class TestSystemChanges:
    def test_the_nftables_table_is_recorded(self, journal: Journal) -> None:
        journal.record_nft_table()
        assert journal.load().nft_table is True

        journal.clear_nft_table()
        assert journal.load().nft_table is False

    def test_reconfigured_links_are_recorded(self, journal: Journal) -> None:
        journal.record_link("eth0")
        journal.record_link("wlan0")
        journal.record_link("eth0")

        assert journal.load().links == ["eth0", "wlan0"]

    def test_restore_reports_what_it_still_has_to_undo(self, journal: Journal) -> None:
        journal.record_nft_table()
        journal.record_link("eth0")

        pending = journal.load()
        assert pending.nft_table
        assert pending.links == ["eth0"]
