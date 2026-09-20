"""Disabling DNS-over-HTTPS without trampling other policies (SPEC 8.2)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from anchor.blocker.commands import RecordingRunner
from anchor.blocker.journal import Journal
from anchor.blocker.policies import apply_policies
from anchor.blocker.restore import restore_everything


@pytest.fixture
def journal(tmp_path: Path) -> Journal:
    return Journal(tmp_path / "applied.json")


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "root"


def install(root: Path, executable: str) -> None:
    target = root / executable.lstrip("/")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("#!/bin/sh\n", encoding="utf-8")


def policy_at(root: Path, path: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((root / path.lstrip("/")).read_text(encoding="utf-8"))
    return loaded


class TestFindingBrowsers:
    def test_nothing_installed_means_nothing_written(self, journal: Journal, root: Path) -> None:
        assert apply_policies(journal, root=root) == []
        assert journal.load().files == []

    def test_firefox_is_found(self, journal: Journal, root: Path) -> None:
        install(root, "/usr/bin/firefox")
        assert apply_policies(journal, root=root) == ["Firefox"]

    def test_the_firefox_snap_is_found(self, journal: Journal, root: Path) -> None:
        """Ubuntu's default browser is the Snap (spike 6)."""
        install(root, "/snap/firefox/current/usr/lib/firefox/firefox")
        assert apply_policies(journal, root=root) == ["Firefox"]

    def test_several_browsers_are_all_configured(self, journal: Journal, root: Path) -> None:
        install(root, "/usr/bin/firefox")
        install(root, "/opt/google/chrome/chrome")
        install(root, "/usr/bin/chromium")

        assert apply_policies(journal, root=root) == ["Firefox", "Google Chrome", "Chromium"]


class TestFirefox:
    def test_doh_is_disabled_and_locked(self, journal: Journal, root: Path) -> None:
        """Locked, or the user could simply turn it back on."""
        install(root, "/usr/bin/firefox")
        apply_policies(journal, root=root)

        doh = policy_at(root, "/etc/firefox/policies/policies.json")["policies"]["DNSOverHTTPS"]
        assert doh == {"Enabled": False, "Locked": True}

    def test_existing_policies_survive(self, journal: Journal, root: Path) -> None:
        """Firefox has one policy file and other tools write to it."""
        install(root, "/usr/bin/firefox")
        existing = root / "etc/firefox/policies/policies.json"
        existing.parent.mkdir(parents=True)
        existing.write_text(
            json.dumps({"policies": {"DisableTelemetry": True, "DontCheckDefaultBrowser": True}}),
            encoding="utf-8",
        )

        apply_policies(journal, root=root)

        policies = policy_at(root, "/etc/firefox/policies/policies.json")["policies"]
        assert policies["DisableTelemetry"] is True
        assert policies["DontCheckDefaultBrowser"] is True
        assert policies["DNSOverHTTPS"]["Enabled"] is False

    def test_keys_outside_policies_survive(self, journal: Journal, root: Path) -> None:
        install(root, "/usr/bin/firefox")
        existing = root / "etc/firefox/policies/policies.json"
        existing.parent.mkdir(parents=True)
        existing.write_text(
            json.dumps({"comment": "hand written", "policies": {}}), encoding="utf-8"
        )

        apply_policies(journal, root=root)

        assert policy_at(root, "/etc/firefox/policies/policies.json")["comment"] == "hand written"

    def test_a_corrupt_file_does_not_stop_the_block(self, journal: Journal, root: Path) -> None:
        install(root, "/usr/bin/firefox")
        existing = root / "etc/firefox/policies/policies.json"
        existing.parent.mkdir(parents=True)
        existing.write_text("{ not json at all", encoding="utf-8")

        assert apply_policies(journal, root=root) == ["Firefox"]
        doh = policy_at(root, "/etc/firefox/policies/policies.json")["policies"]["DNSOverHTTPS"]
        assert doh["Enabled"] is False

    def test_a_corrupt_file_is_still_restored_as_it_was(self, journal: Journal, root: Path) -> None:
        install(root, "/usr/bin/firefox")
        existing = root / "etc/firefox/policies/policies.json"
        existing.parent.mkdir(parents=True)
        existing.write_text("{ not json at all", encoding="utf-8")

        apply_policies(journal, root=root)
        restore_everything(journal, runner=RecordingRunner())

        assert existing.read_text(encoding="utf-8") == "{ not json at all"


class TestChromiumFamily:
    def test_anchor_writes_its_own_file(self, journal: Journal, root: Path) -> None:
        """A directory of policies, so nothing else has to be touched."""
        install(root, "/usr/bin/chromium")
        apply_policies(journal, root=root)

        policy = policy_at(root, "/etc/chromium/policies/managed/anchor.json")
        assert policy["DnsOverHttpsMode"] == "off"

    def test_the_built_in_resolver_is_disabled_too(self, journal: Journal, root: Path) -> None:
        """With it on, Chrome can bypass the system stub even without DoH."""
        install(root, "/usr/bin/chromium")
        apply_policies(journal, root=root)

        policy = policy_at(root, "/etc/chromium/policies/managed/anchor.json")
        assert policy["BuiltInDnsClientEnabled"] is False

    def test_another_tools_policy_is_left_alone(self, journal: Journal, root: Path) -> None:
        install(root, "/usr/bin/chromium")
        neighbour = root / "etc/chromium/policies/managed/site-policy.json"
        neighbour.parent.mkdir(parents=True)
        neighbour.write_text('{"HomepageLocation": "https://example.com"}', encoding="utf-8")

        apply_policies(journal, root=root)
        restore_everything(journal, runner=RecordingRunner())

        assert neighbour.exists(), "another tool's policy was removed"


class TestRestoring:
    def test_a_policy_anchor_created_is_removed(self, journal: Journal, root: Path) -> None:
        install(root, "/usr/bin/chromium")
        apply_policies(journal, root=root)
        target = root / "etc/chromium/policies/managed/anchor.json"
        assert target.exists()

        restore_everything(journal, runner=RecordingRunner())
        assert not target.exists()

    def test_an_existing_policy_comes_back_untouched(self, journal: Journal, root: Path) -> None:
        install(root, "/usr/bin/firefox")
        existing = root / "etc/firefox/policies/policies.json"
        existing.parent.mkdir(parents=True)
        original = json.dumps({"policies": {"DisableTelemetry": True}}, indent=2)
        existing.write_text(original, encoding="utf-8")

        apply_policies(journal, root=root)
        restore_everything(journal, runner=RecordingRunner())

        assert existing.read_text(encoding="utf-8") == original
