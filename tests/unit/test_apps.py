"""Finding the applications installed on this machine (SPEC 9)."""

from __future__ import annotations

from pathlib import Path

from anchor.blocker.apps import AppKind, discover, parse_desktop_entry

NATIVE = """\
[Desktop Entry]
Type=Application
Name=Discord
Name[es]=Discord
Comment=All-in-one voice and text chat
Exec=/usr/bin/discord %U
Icon=discord
Categories=Network;InstantMessaging;
"""

SNAP = """\
[Desktop Entry]
Type=Application
Name=Spotify
Exec=env BAMF_DESKTOP_FILE_HINT=/var/lib/snapd/desktop/applications/spotify_spotify.desktop \
 /snap/bin/spotify %U
Icon=/snap/spotify/current/usr/share/spotify/icons/spotify-linux-128.png
"""

FLATPAK = """\
[Desktop Entry]
Type=Application
Name=Steam
Exec=/usr/bin/flatpak run --branch=stable --arch=x86_64 com.valvesoftware.Steam @@u %U @@
Icon=com.valvesoftware.Steam
X-Flatpak=com.valvesoftware.Steam
"""


def write(directory: Path, name: str, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(body, encoding="utf-8")
    return path


class TestReadingOneEntry:
    def test_a_native_application(self, tmp_path: Path) -> None:
        app = parse_desktop_entry(write(tmp_path, "discord.desktop", NATIVE))

        assert app is not None
        assert app.name == "Discord"
        assert app.kind is AppKind.NATIVE
        assert app.exec_path == "/usr/bin/discord"
        assert app.id == "discord.desktop"

    def test_field_codes_are_stripped_from_the_command(self, tmp_path: Path) -> None:
        """%U and friends are placeholders, not part of the path."""
        app = parse_desktop_entry(write(tmp_path, "discord.desktop", NATIVE))
        assert app is not None
        assert "%" not in app.exec_path

    def test_a_snap(self, tmp_path: Path) -> None:
        app = parse_desktop_entry(write(tmp_path, "spotify_spotify.desktop", SNAP))

        assert app is not None
        assert app.kind is AppKind.SNAP
        assert app.snap_name == "spotify"
        assert app.exec_path == "/snap/bin/spotify"

    def test_an_env_prefix_is_not_mistaken_for_the_program(self, tmp_path: Path) -> None:
        """Snap entries start with `env VAR=...`, which is not the binary."""
        app = parse_desktop_entry(write(tmp_path, "spotify_spotify.desktop", SNAP))
        assert app is not None
        assert app.exec_path != "env"

    def test_a_flatpak(self, tmp_path: Path) -> None:
        app = parse_desktop_entry(write(tmp_path, "com.valvesoftware.Steam.desktop", FLATPAK))

        assert app is not None
        assert app.kind is AppKind.FLATPAK
        assert app.flatpak_id == "com.valvesoftware.Steam"

    def test_a_flatpak_is_recognised_from_its_command(self, tmp_path: Path) -> None:
        """Not every exported entry carries the X-Flatpak key."""
        body = FLATPAK.replace("X-Flatpak=com.valvesoftware.Steam\n", "")
        app = parse_desktop_entry(write(tmp_path, "steam.desktop", body))

        assert app is not None
        assert app.kind is AppKind.FLATPAK
        assert app.flatpak_id == "com.valvesoftware.Steam"


class TestWhatIsSkipped:
    def test_hidden_entries(self, tmp_path: Path) -> None:
        body = NATIVE + "NoDisplay=true\n"
        assert parse_desktop_entry(write(tmp_path, "hidden.desktop", body)) is None

    def test_entries_marked_hidden(self, tmp_path: Path) -> None:
        body = NATIVE + "Hidden=true\n"
        assert parse_desktop_entry(write(tmp_path, "gone.desktop", body)) is None

    def test_things_that_are_not_applications(self, tmp_path: Path) -> None:
        body = "[Desktop Entry]\nType=Link\nName=Somewhere\nURL=https://example.com\n"
        assert parse_desktop_entry(write(tmp_path, "link.desktop", body)) is None

    def test_entries_with_no_command(self, tmp_path: Path) -> None:
        body = "[Desktop Entry]\nType=Application\nName=Nothing\n"
        assert parse_desktop_entry(write(tmp_path, "empty.desktop", body)) is None

    def test_a_file_that_is_not_a_desktop_entry(self, tmp_path: Path) -> None:
        assert parse_desktop_entry(write(tmp_path, "junk.desktop", "not ini at all")) is None

    def test_an_unreadable_file_costs_one_entry(self, tmp_path: Path) -> None:
        assert parse_desktop_entry(tmp_path / "missing.desktop") is None


class TestDiscovery:
    def test_every_location_is_searched(self, tmp_path: Path) -> None:
        write(tmp_path / "usr/share/applications", "discord.desktop", NATIVE)
        write(tmp_path / "var/lib/snapd/desktop/applications", "spotify_spotify.desktop", SNAP)
        write(
            tmp_path / "var/lib/flatpak/exports/share/applications",
            "com.valvesoftware.Steam.desktop",
            FLATPAK,
        )

        found = discover(roots=[tmp_path])
        assert {app.name for app in found} == {"Discord", "Spotify", "Steam"}

    def test_results_are_sorted_by_name(self, tmp_path: Path) -> None:
        """The interface shows a list a person has to scan (SPEC 14)."""
        write(tmp_path / "usr/share/applications", "discord.desktop", NATIVE)
        write(tmp_path / "usr/share/applications", "steam.desktop", FLATPAK)

        assert [app.name for app in discover(roots=[tmp_path])] == ["Discord", "Steam"]

    def test_a_user_entry_wins_over_a_system_one(self, tmp_path: Path) -> None:
        """The same id in ~/.local overrides /usr/share, as XDG says."""
        write(tmp_path / "usr/share/applications", "discord.desktop", NATIVE)
        write(
            tmp_path / "home/.local/share/applications",
            "discord.desktop",
            NATIVE.replace("Name=Discord", "Name=Discord (mine)"),
        )

        found = discover(roots=[tmp_path], home=tmp_path / "home")
        assert [app.name for app in found] == ["Discord (mine)"]

    def test_missing_directories_are_not_an_error(self, tmp_path: Path) -> None:
        assert discover(roots=[tmp_path / "nowhere"]) == []

    def test_the_same_application_is_listed_once(self, tmp_path: Path) -> None:
        write(tmp_path / "usr/share/applications", "discord.desktop", NATIVE)
        write(tmp_path / "usr/local/share/applications", "discord.desktop", NATIVE)

        assert len(discover(roots=[tmp_path])) == 1


class TestResolvingTheCommand:
    """A relative name cannot be compared against a running process (SPEC 9)."""

    def test_a_bare_name_becomes_a_path(self, tmp_path: Path) -> None:
        body = "[Desktop Entry]\nType=Application\nName=Shell\nExec=sh\n"
        app = parse_desktop_entry(write(tmp_path, "sh.desktop", body))

        assert app is not None
        assert app.exec_path.startswith("/")
        assert app.exec_path.endswith("/sh")

    def test_an_absolute_path_is_left_alone(self, tmp_path: Path) -> None:
        app = parse_desktop_entry(write(tmp_path, "discord.desktop", NATIVE))

        assert app is not None
        assert app.exec_path == "/usr/bin/discord"

    def test_something_not_on_the_path_is_kept_as_written(self, tmp_path: Path) -> None:
        """Better a name that matches nothing than an entry that vanishes."""
        body = "[Desktop Entry]\nType=Application\nName=Ghost\nExec=definitely-not-installed\n"
        app = parse_desktop_entry(write(tmp_path, "ghost.desktop", body))

        assert app is not None
        assert app.exec_path == "definitely-not-installed"
