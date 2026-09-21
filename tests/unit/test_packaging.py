"""Every shipped data file reaches every package (SPEC 19).

A list Anchor ships is useless if the package leaves it out, and the three
packaging formats are edited by hand and never built in CI. These are cheap
checks against the drift that is otherwise only found by installing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"

DEB = ROOT / "packaging" / "deb" / "debian" / "anchor.install"
RPM = ROOT / "packaging" / "rpm" / "anchor.spec"
ARCH = ROOT / "packaging" / "arch" / "PKGBUILD"


def data_files() -> list[Path]:
    return sorted(path for path in DATA.rglob("*") if path.is_file())


@pytest.mark.parametrize("packaging", [DEB, RPM, ARCH], ids=lambda path: path.parent.name)
class TestEveryFileIsInstalled:
    def test_the_lists_are_named(self, packaging: Path) -> None:
        text = packaging.read_text(encoding="utf-8")

        for path in data_files():
            relative = path.relative_to(ROOT).as_posix()
            # Either named outright, or covered by its directory's glob.
            covered = relative in text or f"{path.parent.relative_to(ROOT).as_posix()}/*" in text
            assert covered, f"{packaging.name} does not install {relative}"

    def test_the_users_category_directory_is_created(self, packaging: Path) -> None:
        """SPEC 12: the user's own copies need somewhere to go."""
        text = packaging.read_text(encoding="utf-8")
        if packaging is DEB:
            text += (DEB.parent / "anchor.dirs").read_text(encoding="utf-8")

        assert "anchor/categories" in text


class TestTheRpmSpecIsShaped:
    """%install runs as a shell script; a stray %files line would break it."""

    SECTIONS = (
        "prep",
        "build",
        "install",
        "check",
        "files",
        "post",
        "preun",
        "postun",
        "changelog",
    )

    def sections(self) -> dict[str, list[str]]:
        found: dict[str, list[str]] = {}
        current = ""
        for line in RPM.read_text(encoding="utf-8").splitlines():
            head = line.split(maxsplit=1)[0] if line.startswith("%") else ""
            if head[1:] in self.SECTIONS:
                current = head
                found[current] = []
            elif current:
                found[current].append(line)
        return found

    def test_install_contains_no_file_list_entries(self) -> None:
        for line in self.sections().get("%install", []):
            assert not line.startswith(
                "%{_datadir}"
            ), f"a %files entry is in %install and would be run as a command: {line}"

    def test_the_files_section_lists_the_categories(self) -> None:
        assert any(
            "categories" in line for line in self.sections().get("%files", [])
        ), "the categories are installed but not packaged"


@pytest.mark.parametrize("packaging", [DEB, RPM, ARCH], ids=lambda path: path.parent.name)
class TestTheTranslationsAreInstalled:
    """SPEC 14: Spanish is half the interface, and it is a build step."""

    def text(self, packaging: Path) -> str:
        extra = ""
        if packaging is DEB:
            extra = (DEB.parent / "rules").read_text(encoding="utf-8")
        return packaging.read_text(encoding="utf-8") + extra

    def test_the_catalogues_are_compiled(self, packaging: Path) -> None:
        assert "tools/po.py compile" in self.text(packaging)

    def test_and_land_where_gettext_looks(self, packaging: Path) -> None:
        assert "LC_MESSAGES/anchor.mo" in self.text(packaging)


class TestTheDesktopEntry:
    """Without it, nothing in the menu starts the interface (SPEC 14, 17)."""

    def test_it_names_the_program_that_exists(self) -> None:
        entry = (ROOT / "data" / "applications" / "org.anchor.Anchor.desktop").read_text(
            encoding="utf-8"
        )
        assert "Exec=anchor-gui" in entry

    def test_the_program_is_an_entry_point(self) -> None:
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        assert "anchor-gui = " in pyproject
