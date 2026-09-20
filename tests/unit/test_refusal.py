"""Refusing a manual stop while a session runs (SPEC 5.3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from anchor.engine.refusal import (
    DROP_IN_NAME,
    UNITS,
    apply_refusal,
    is_applied,
    remove_refusal,
)
from anchor.system.commands import RecordingRunner, Result


@pytest.fixture
def runtime(tmp_path: Path) -> Path:
    return tmp_path / "run" / "systemd" / "system"


def drop_in(runtime: Path, unit: str) -> Path:
    return runtime / f"{unit}.d" / DROP_IN_NAME


class TestApplying:
    def test_both_units_are_covered(self, runtime: Path) -> None:
        """Refusing only the engine would leave the blocker stoppable."""
        apply_refusal(runtime_dir=runtime, runner=RecordingRunner())

        for unit in UNITS:
            assert drop_in(runtime, unit).exists()

    def test_the_setting_is_what_systemd_reads(self, runtime: Path) -> None:
        apply_refusal(runtime_dir=runtime, runner=RecordingRunner())

        body = drop_in(runtime, UNITS[0]).read_text(encoding="utf-8")
        assert "[Unit]" in body
        assert "RefuseManualStop=yes" in body

    def test_systemd_is_reloaded(self, runtime: Path) -> None:
        """Without a reload the drop-in exists but means nothing."""
        runner = RecordingRunner()
        apply_refusal(runtime_dir=runtime, runner=runner)

        assert runner.ran("systemctl daemon-reload")

    def test_it_goes_under_run_not_etc(self, runtime: Path) -> None:
        """In /etc it would survive a reboot and block uninstalling."""
        apply_refusal(runtime_dir=runtime, runner=RecordingRunner())
        assert "/run/" in str(drop_in(runtime, UNITS[0]))

    def test_applying_twice_changes_nothing(self, runtime: Path) -> None:
        runner = RecordingRunner()
        assert apply_refusal(runtime_dir=runtime, runner=runner) is True
        assert apply_refusal(runtime_dir=runtime, runner=runner) is False

        assert sum(1 for call in runner.calls if "daemon-reload" in call) == 1

    def test_a_reload_that_fails_does_not_pretend_to_have_worked(self, runtime: Path) -> None:
        runner = RecordingRunner({"daemon-reload": Result(code=1, err="no systemd here")})
        assert apply_refusal(runtime_dir=runtime, runner=runner) is False


class TestRemoving:
    def test_the_drop_ins_go(self, runtime: Path) -> None:
        apply_refusal(runtime_dir=runtime, runner=RecordingRunner())
        remove_refusal(runtime_dir=runtime, runner=RecordingRunner())

        for unit in UNITS:
            assert not drop_in(runtime, unit).exists()

    def test_the_directories_go_too(self, runtime: Path) -> None:
        apply_refusal(runtime_dir=runtime, runner=RecordingRunner())
        remove_refusal(runtime_dir=runtime, runner=RecordingRunner())

        assert not (runtime / f"{UNITS[0]}.d").exists()

    def test_systemd_is_reloaded(self, runtime: Path) -> None:
        apply_refusal(runtime_dir=runtime, runner=RecordingRunner())
        runner = RecordingRunner()
        remove_refusal(runtime_dir=runtime, runner=runner)

        assert runner.ran("systemctl daemon-reload")

    def test_removing_nothing_is_harmless(self, runtime: Path) -> None:
        """This runs every time a session ends, including when none was active."""
        runner = RecordingRunner()
        assert remove_refusal(runtime_dir=runtime, runner=runner) is False
        assert not runner.ran("daemon-reload")

    def test_a_directory_with_someone_elses_drop_in_survives(self, runtime: Path) -> None:
        apply_refusal(runtime_dir=runtime, runner=RecordingRunner())
        neighbour = runtime / f"{UNITS[0]}.d" / "99-local.conf"
        neighbour.write_text("[Service]\nNice=5\n", encoding="utf-8")

        remove_refusal(runtime_dir=runtime, runner=RecordingRunner())

        assert neighbour.exists(), "an administrator's own drop-in was deleted"


class TestReporting:
    def test_is_applied_tells_the_truth(self, runtime: Path) -> None:
        assert not is_applied(runtime)
        apply_refusal(runtime_dir=runtime, runner=RecordingRunner())
        assert is_applied(runtime)

    def test_half_applied_counts_as_not_applied(self, runtime: Path) -> None:
        """A session that can stop the blocker is not protected."""
        apply_refusal(runtime_dir=runtime, runner=RecordingRunner())
        drop_in(runtime, UNITS[1]).unlink()

        assert not is_applied(runtime)


class TestRelocation:
    def test_a_relocated_tree_moves_the_systemd_path_too(self, tmp_path: Path) -> None:
        """Otherwise a test or a developer can lock the real machine's services."""
        from anchor.engine.paths import Paths

        relocated = Paths.resolve(tmp_path)

        assert str(relocated.systemd_runtime_dir).startswith(str(tmp_path))
        assert "run/systemd/system" in str(relocated.systemd_runtime_dir)

    def test_the_installed_tree_uses_the_real_path(self) -> None:
        from anchor.engine.paths import Paths

        assert Paths().systemd_runtime_dir == Path("/run/systemd/system")
