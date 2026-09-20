"""Pointing systemd-resolved at Anchor, and noticing the network move (SPEC 8.2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from anchor.blocker.commands import RecordingRunner, Result
from anchor.blocker.journal import Journal
from anchor.blocker.resolved import (
    NetworkState,
    apply,
    follow_network_changes,
    is_available,
    read_state,
)

STATUS = """\
Global
         Protocols: LLMNR=resolve -mDNS -DNSOverTLS
  resolv.conf mode: stub

Link 2 (eth0)
    Current Scopes: DNS
         Protocols: +DefaultRoute
       DNS Servers: 192.168.1.1 192.168.1.2
        DNS Domain: lan

Link 3 (wlan0)
    Current Scopes: DNS
       DNS Servers: 10.0.0.1

Link 1 (lo)
    Current Scopes: none
"""


@pytest.fixture
def journal(tmp_path: Path) -> Journal:
    return Journal(tmp_path / "applied.json")


@pytest.fixture
def drop_in(tmp_path: Path) -> Path:
    return tmp_path / "run" / "systemd" / "resolved.conf.d" / "50-anchor.conf"


def runner_with_status(status: str = STATUS) -> RecordingRunner:
    return RecordingRunner({"resolvectl status": Result(code=0, out=status)})


class TestReadingTheNetwork:
    def test_links_are_found(self) -> None:
        assert read_state(runner=runner_with_status()).links == ["eth0", "wlan0"]

    def test_loopback_is_ignored(self) -> None:
        assert "lo" not in read_state(runner=runner_with_status()).links

    def test_every_upstream_is_collected(self) -> None:
        state = read_state(runner=runner_with_status())
        assert state.upstreams == ["192.168.1.1", "192.168.1.2", "10.0.0.1"]

    def test_anchors_own_address_is_never_an_upstream(self) -> None:
        """Otherwise the resolver forwards to itself and queries spin."""
        status = STATUS.replace("10.0.0.1", "127.0.0.1:5391")
        state = read_state(runner=runner_with_status(status))

        assert "127.0.0.1:5391" not in state.upstreams
        assert state.upstreams == ["192.168.1.1", "192.168.1.2"]

    def test_a_machine_without_resolved_reads_as_empty(self) -> None:
        runner = RecordingRunner({"resolvectl": Result(code=127, err="not found")})
        state = read_state(runner=runner)

        assert state.links == []
        assert state.upstreams == []


class TestAvailability:
    def test_present_and_running(self) -> None:
        runner = RecordingRunner({"is-active": Result(code=0, out="active")})
        assert is_available(runner=runner)

    def test_present_but_stopped(self) -> None:
        runner = RecordingRunner({"is-active": Result(code=3, out="inactive")})
        assert not is_available(runner=runner)

    def test_absent(self) -> None:
        runner = RecordingRunner({"resolvectl": Result(code=127, err="not found")})
        assert not is_available(runner=runner)


class TestApplying:
    def test_the_upstreams_are_read_before_anything_changes(
        self, journal: Journal, drop_in: Path
    ) -> None:
        runner = runner_with_status()
        state = apply(journal, runner=runner, resolver_port=5391, drop_in=drop_in)

        assert state.upstreams == ["192.168.1.1", "192.168.1.2", "10.0.0.1"]
        status_at = next(i for i, c in enumerate(runner.calls) if "status" in c)
        change_at = next(i for i, c in enumerate(runner.calls) if "dns" in c)
        assert status_at < change_at

    def test_every_link_is_pointed_at_anchor(self, journal: Journal, drop_in: Path) -> None:
        runner = runner_with_status()
        apply(journal, runner=runner, resolver_port=5391, drop_in=drop_in)

        assert runner.ran("resolvectl dns eth0 127.0.0.1:5391")
        assert runner.ran("resolvectl dns wlan0 127.0.0.1:5391")

    def test_each_link_gets_the_catch_all_routing_domain(
        self, journal: Journal, drop_in: Path
    ) -> None:
        """Without ~. the link only answers for its own search domain."""
        runner = runner_with_status()
        apply(journal, runner=runner, resolver_port=5391, drop_in=drop_in)

        assert runner.ran("resolvectl domain eth0 ~.")

    def test_configured_links_are_recorded_for_restoration(
        self, journal: Journal, drop_in: Path
    ) -> None:
        apply(journal, runner=runner_with_status(), resolver_port=5391, drop_in=drop_in)
        assert journal.load().links == ["eth0", "wlan0"]

    def test_a_link_that_refuses_is_not_recorded(self, journal: Journal, drop_in: Path) -> None:
        """Restoration must not revert a link Anchor never changed."""
        runner = RecordingRunner(
            {
                "resolvectl status": Result(code=0, out=STATUS),
                "resolvectl dns wlan0": Result(code=1, err="Link wlan0 is unmanaged"),
            }
        )
        apply(journal, runner=runner, resolver_port=5391, drop_in=drop_in)

        assert journal.load().links == ["eth0"]

    def test_the_drop_in_goes_under_run(self, journal: Journal, drop_in: Path) -> None:
        """A drop-in in /etc could leave a machine unable to resolve (P4)."""
        apply(journal, runner=runner_with_status(), resolver_port=5391, drop_in=drop_in)

        assert drop_in.exists()
        assert "/run/" in str(drop_in)

    def test_caching_is_turned_off(self, journal: Journal, drop_in: Path) -> None:
        """A cached answer is a query Anchor never sees."""
        apply(journal, runner=runner_with_status(), resolver_port=5391, drop_in=drop_in)

        assert "Cache=no" in drop_in.read_text(encoding="utf-8")

    def test_the_cache_is_flushed(self, journal: Journal, drop_in: Path) -> None:
        runner = runner_with_status()
        apply(journal, runner=runner, resolver_port=5391, drop_in=drop_in)

        assert runner.ran("resolvectl flush-caches")

    def test_the_drop_in_is_recorded_so_it_can_be_removed(
        self, journal: Journal, drop_in: Path
    ) -> None:
        apply(journal, runner=runner_with_status(), resolver_port=5391, drop_in=drop_in)

        assert [Path(f.path) for f in journal.load().files] == [drop_in]


class TestFollowingTheNetwork:
    def test_an_unchanged_network_is_left_alone(self, journal: Journal, drop_in: Path) -> None:
        runner = runner_with_status()
        before = read_state(runner=runner)

        assert follow_network_changes(journal, before, runner=runner, drop_in=drop_in) is None

    def test_a_new_link_triggers_a_reapply(self, journal: Journal, drop_in: Path) -> None:
        """SPEC 3: a laptop that changes networks must work without manual action."""
        before = NetworkState(links=["eth0"], upstreams=["192.168.1.1"])
        runner = runner_with_status()

        after = follow_network_changes(journal, before, runner=runner, drop_in=drop_in)

        assert after is not None
        assert after.links == ["eth0", "wlan0"]
        assert runner.ran("resolvectl dns wlan0")

    def test_a_changed_server_triggers_a_reapply(self, journal: Journal, drop_in: Path) -> None:
        """Moving from home to a cafe keeps the link and changes the server."""
        before = NetworkState(links=["eth0", "wlan0"], upstreams=["192.168.1.1"])
        runner = runner_with_status()

        assert follow_network_changes(journal, before, runner=runner, drop_in=drop_in) is not None

    def test_a_lost_link_triggers_a_reapply(self, journal: Journal, drop_in: Path) -> None:
        before = NetworkState(
            links=["eth0", "wlan0", "usb0"],
            upstreams=["192.168.1.1", "192.168.1.2", "10.0.0.1"],
        )
        runner = runner_with_status()

        assert follow_network_changes(journal, before, runner=runner, drop_in=drop_in) is not None
